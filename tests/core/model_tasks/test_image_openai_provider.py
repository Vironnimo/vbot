"""Tests for image openai provider."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from core.model_tasks.image_providers import (
    _OPENAI_IMAGE_KEYS,
    ProviderImageClient,
    _build_openai_image_payload,
)
from core.model_tasks.image_types import ImageGenerationResult, ImageInput
from core.providers.errors import (
    ProviderError,
    ProviderOutcomeUnknownError,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from tests.core.model_tasks.image_providers_test_support import (
    OPENROUTER_IMAGES_URL,
    _openrouter_image_client,
    _unified_image_response,
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


def test_build_openai_payload_adds_non_conflicting_extra_options() -> None:
    """The escape hatch also applies to the OpenAI native path."""

    payload = _build_openai_image_payload(
        "gpt-image-2",
        "a cat",
        {
            "size": "1024x1024",
            "extra_options": {"partial_images": 2},
        },
    )

    assert payload["size"] == "1024x1024"
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


    respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(200, json={"created": 1, "data": []})
    )
    client = _openai_image_client("gpt-image-1")

    with pytest.raises(ProviderError):
        await client.generate("a cat", options={})


@pytest.mark.asyncio
@respx.mock
async def test_openai_image_generate_does_not_retry_invalid_b64_json() -> None:
    route = respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": "not-base64!"}]},
        )
    )
    client = _openai_image_client("gpt-image-1")

    with pytest.raises(ProviderOutcomeUnknownError) as exc_info:
        await client.generate("a cat", options={})

    assert exc_info.value.retryable is False
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_openai_image_generate_url_response_is_a_provider_error() -> None:
    """When OpenAI returns ``url`` entries (response_format=url) the wire
    layer raises a non-retryable error rather than silently dropping the
    image. The Settings schema defaults ``response_format`` to ``b64_json``;
    a user who explicitly chose ``url`` sees a clear actionable error."""


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
    """A provider using an unsupported task adapter surfaces an explicit
    ``ProviderError`` so the caller (ImageService) can map it to an
    ``ImageExecutionError``."""


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


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "output_format,media_type,extension",
    [
        ("svg", "image/svg+xml", ".svg"),
        ("png", "image/png", ".png"),
        ("webp", "image/webp", ".webp"),
    ],
)
async def test_image_output_format_survives_artifact_storage(
    tmp_path,
    monkeypatch,
    output_format,
    media_type,
    extension,
) -> None:
    from typing import Any, cast

    from core.model_tasks.image import ImageService

    image_bytes = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    respx.post(OPENROUTER_IMAGES_URL).mock(
        return_value=httpx.Response(200, json=_unified_image_response(image_bytes))
    )
    client = _openrouter_image_client("recraft/recraft-v4-vector")
    result = await client.generate("a cat", options={"output_format": output_format})
    assert result.media_type == media_type
    service = ImageService(cast(Any, object()), cast(Any, object()))

    async def generate(*args, **kwargs):
        return result

    monkeypatch.setattr(service, "generate", generate)
    (artifact,) = await service.generate_artifacts("a cat", output_dir=tmp_path)
    assert artifact.media_type == media_type
    assert artifact.file_path.suffix == extension
    assert artifact.file_path.read_bytes() == image_bytes
