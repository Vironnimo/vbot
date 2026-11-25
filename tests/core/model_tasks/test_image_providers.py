"""Model tasks: the OpenRouter, OpenAI and Google image wires of ``ProviderImageClient``.

Every case goes through the public ``generate`` against a mocked endpoint:
the request the Provider receives, the images the caller gets back, and how
unusable responses fail. Generation may be billed, so only a documented
"not processed" status is retried.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from core.model_tasks.image_profile import image_wire
from core.model_tasks.image_providers import ProviderImageClient
from core.model_tasks.image_types import ImageInput
from core.models import ModelRegistry
from core.providers.errors import (
    ProviderContentRefusedError,
    ProviderError,
    ProviderOutcomeUnknownError,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig, ProviderRegistry
from tests.core.model_tasks.image_providers_test_support import (
    GOOGLE_BASE,
    OPENROUTER_IMAGES_URL,
    _google_image_client,
    _openai_image_client,
    _openrouter_image_client,
    _unified_image_response,
)

OPENAI_GENERATIONS_URL = "https://api.openai.com/v1/images/generations"
OPENAI_EDITS_URL = "https://api.openai.com/v1/images/edits"
_OPENROUTER_MODEL = "black-forest-labs/flux.2-pro"
_OPENAI_MODEL = "gpt-image-1"
_GOOGLE_MODEL = "gemini-3.1-flash-image"
GOOGLE_GENERATE_URL = f"{GOOGLE_BASE}/models/{_GOOGLE_MODEL}:generateContent"


def _client(wire: str) -> ProviderImageClient:
    if wire == "openrouter":
        return _openrouter_image_client(_OPENROUTER_MODEL)
    if wire == "google":
        return _google_image_client(_GOOGLE_MODEL)
    return _openai_image_client(_OPENAI_MODEL)


def _url(wire: str) -> str:
    return {"openrouter": OPENROUTER_IMAGES_URL, "google": GOOGLE_GENERATE_URL}.get(
        wire, OPENAI_GENERATIONS_URL
    )


def _model(wire: str) -> str:
    return _OPENROUTER_MODEL if wire == "openrouter" else _OPENAI_MODEL


def _google_answer(*parts: dict[str, Any], finish: str = "STOP", **fields: Any) -> dict[str, Any]:
    """Build a Gemini ``generateContent`` answer with one candidate."""

    candidate = {"content": {"role": "model", "parts": list(parts)}, "finishReason": finish}
    if "finishMessage" in fields:
        candidate["finishMessage"] = fields.pop("finishMessage")
    return {"candidates": [candidate], **fields}


def _inline_image(data: bytes, mime_type: str = "image/png", **fields: Any) -> dict[str, Any]:
    encoded = base64.b64encode(data).decode()
    return {"inlineData": {"mimeType": mime_type, "data": encoded}, **fields}


@pytest.mark.parametrize(
    "model_id,path",
    [
        ("gemini-nano-banana-2.1", "google/v1beta/models/gemini-nano-banana-2.1:generateContent"),
        ("gpt-image-2.5-sunburst", "openai/v1/images/edits"),
        ("grok-imagine-image-2.0", "xai/v1/images/edits"),
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_console_edits_use_the_catalog_native_image_wire(model_id: str, path: str) -> None:
    resources = Path(__file__).resolve().parents[3] / "resources"
    provider = ProviderRegistry.load(resources).get("opencode-zen")
    model = ModelRegistry.load(resources).get(provider.id, model_id)
    wire = image_wire(provider.id, provider.adapter, None, model)
    client = ProviderImageClient(
        provider=provider,
        connection=provider.get_connection("api-key"),
        credential="test",
        model_id=model_id,
    )
    output: dict[str, Any] = (
        _google_answer(_inline_image(b"test"))
        if wire == "google"
        else {"data": [{"b64_json": "dGVzdA==", "mime_type": "image/png"}]}
    )
    output.update({"cost": 0.008, "opencode": {"list_cost": 0.01, "billing_source": "balance"}})
    route = respx.post("https://opencode.ai/inference/" + path).respond(200, json=output)
    inputs = (ImageInput("a.png", "image/png", b"one"), ImageInput("b.png", "image/png", b"two"))
    result = await client.generate("edit", options={}, input_images=inputs, wire=wire)
    assert result.usage and result.usage["cost"] == {
        "amount_usd": 0.008,
        "source": "provider",
        "billing_source": "balance",
        "list_cost_usd": 0.01,
    }
    assert result.images[0].data == b"test" and result.images[0].media_type == "image/png"
    request = route.calls[0].request
    if wire == "google":
        assert request.headers["x-goog-api-key"] == "test"
        parts = json.loads(request.content)["contents"][0]["parts"]
        assert parts[2]["inlineData"]["data"] == "dHdv"
    elif wire == "xai_images":
        assert request.headers["Authorization"] == "Bearer test"
        body = json.loads(request.content)
        assert [part["url"] for part in body["images"]] == [
            "data:image/png;base64,b25l",
            "data:image/png;base64,dHdv",
        ]
        assert body["response_format"] == "b64_json"
    else:
        assert request.headers["Authorization"] == "Bearer test"
        assert b'name="image[]"' in request.content


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
    assert tuple(image.data for image in result.images) == (b"edited",)


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
    assert (tuple(image.data for image in result.images), result.images[0].media_type) == (
        (b"edited-png",),
        "image/jpeg",
    )


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

    assert (
        tuple(image.data for image in result.images),
        result.images[0].media_type,
        result.model,
    ) == (images, media_type, _model(wire))
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
        pytest.param(
            "google",
            httpx.Response(200, json={"candidates": []}),
            "no image",
            id="google-no-candidate",
        ),
        # A cut-off answer is no decision by the Model to render nothing.
        pytest.param(
            "google",
            httpx.Response(200, json=_google_answer({"text": "Here is"}, finish="MAX_TOKENS")),
            "MAX_TOKENS",
            id="google-truncated",
        ),
        pytest.param(
            "google",
            httpx.Response(
                200,
                json=_google_answer({"inlineData": {"mimeType": "image/png", "data": "no-b64!"}}),
            ),
            "could not be decoded",
            id="google-undecodable-image",
        ),
        pytest.param(
            "google",
            httpx.Response(500, json={"error": {"code": 500, "status": "INTERNAL"}}),
            "HTTP 500",
            id="google-ambiguous-500",
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


# OpenRouter: no provider was available. Google: the Model is overloaded,
# documented as "wait and retry".
@pytest.mark.parametrize(
    ("wire", "body"),
    [
        pytest.param("openrouter", _unified_image_response(b"img"), id="openrouter"),
        pytest.param("google", _google_answer(_inline_image(b"img")), id="google"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_documented_not_processed_503_is_retried(wire: str, body: dict[str, Any]) -> None:
    route = respx.post(_url(wire)).mock(
        side_effect=[
            httpx.Response(503, text="temporarily unavailable"),
            httpx.Response(200, json=body),
        ]
    )

    result = await _client(wire).generate("a cat", options={})

    assert tuple(image.data for image in result.images) == (b"img",)
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
    assert tuple(image.data for image in result.images) == (b"custom-image",)


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


@pytest.mark.parametrize(
    ("options", "generation_config", "extra_fields"),
    [
        # Google's defaults apply to every option left unset.
        pytest.param({}, {}, {}, id="no-options"),
        pytest.param(
            {
                "aspect_ratio": "21:9",
                "resolution": "512",
                "thinking_level": "high",
                "n": 2,
                "seed": 7,
                "output_format": "webp",
            },
            {
                "imageConfig": {"aspectRatio": "21:9", "imageSize": "512"},
                "thinkingConfig": {"thinkingLevel": "high"},
            },
            {},
            id="every-option-and-no-foreign-key",
        ),
        pytest.param(
            {"aspect_ratio": "", "resolution": "2K", "thinking_level": None},
            {"imageConfig": {"imageSize": "2K"}},
            {},
            id="empty-placeholders-dropped",
        ),
        pytest.param(
            {"extra_options": {"safetySettings": [{"category": "X", "threshold": "Y"}]}},
            {},
            {"safetySettings": [{"category": "X", "threshold": "Y"}]},
            id="extra-options-merged",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_google_request_carries_prompt_sources_and_image_config(
    options: dict[str, Any], generation_config: dict[str, Any], extra_fields: dict[str, Any]
) -> None:
    route = respx.post(GOOGLE_GENERATE_URL).mock(
        return_value=httpx.Response(200, json=_google_answer(_inline_image(b"img")))
    )

    await _client("google").generate(
        "put the cat on the sofa",
        options=options,
        input_images=(
            ImageInput(filename="cat.png", media_type="image/png", data=b"cat"),
            ImageInput(filename="sofa.jpg", media_type="image/jpeg", data=b"sofa"),
        ),
    )

    request = route.calls.last.request
    assert json.loads(request.content) == {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": "put the cat on the sofa"},
                    {"inlineData": {"mimeType": "image/png", "data": "Y2F0"}},
                    {"inlineData": {"mimeType": "image/jpeg", "data": "c29mYQ=="}},
                ],
            }
        ],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], **generation_config},
        **extra_fields,
    }
    assert request.headers["x-goog-api-key"] == "google-test-key"


@respx.mock
@pytest.mark.asyncio
async def test_google_returns_the_final_images_and_records_token_usage() -> None:
    usage_metadata = {
        "promptTokenCount": 12,
        "candidatesTokenCount": 1290,
        "thoughtsTokenCount": 300,
        "totalTokenCount": 1602,
    }
    respx.post(GOOGLE_GENERATE_URL).mock(
        return_value=httpx.Response(
            200,
            json=_google_answer(
                # An interim thought image is a draft, never a result.
                _inline_image(b"draft", thought=True),
                {"text": "Here is your cat.", "thoughtSignature": "c2ln"},
                _inline_image(b"final-png", thoughtSignature="c2ln"),
                _inline_image(b"final-jpeg", "image/jpeg"),
                usageMetadata=usage_metadata,
                modelVersion=_GOOGLE_MODEL,
            ),
        )
    )
    observer = AsyncMock()
    observer.start.return_value = "test-call"

    result = await _google_image_client(_GOOGLE_MODEL, usage_observer=observer).generate(
        "a cat", options={}
    )

    assert [(image.data, image.media_type, image.index) for image in result.images] == [
        (b"final-png", "image/png", 0),
        (b"final-jpeg", "image/jpeg", 1),
    ]
    assert result.usage == observer.finish.await_args.kwargs["usage"]
    assert result.usage is not None
    assert (result.usage["input_tokens"], result.usage["reasoning_tokens"]) == (12, 300)
    assert "inlineData" not in json.dumps(result.raw)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param(
            {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}},
            "Google blocked the prompt (PROHIBITED_CONTENT)",
            id="blocked-prompt",
        ),
        pytest.param(
            _google_answer(finish="IMAGE_SAFETY", finishMessage="Unable to show the image."),
            "Unable to show the image.",
            id="image-safety",
        ),
        pytest.param(
            _google_answer(finish="IMAGE_OTHER"),
            "Google returned no image (IMAGE_OTHER)",
            id="image-other",
        ),
        pytest.param(
            _google_answer({"text": "I can't draw real people."}, finish="NO_IMAGE"),
            "I can't draw real people.",
            id="no-image-with-reason",
        ),
        # The Model answered in words only: its reply is the reason.
        pytest.param(
            _google_answer({"text": "Which cat do you mean?"}),
            "Which cat do you mean?",
            id="text-only",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_google_render_declined_by_the_model_is_a_refusal(
    body: dict[str, Any], reason: str
) -> None:
    route = respx.post(GOOGLE_GENERATE_URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(ProviderContentRefusedError) as raised:
        await _client("google").generate("a cat", options={})

    assert raised.value.reason == reason
    assert route.call_count == 1
