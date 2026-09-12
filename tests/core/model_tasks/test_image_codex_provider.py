"""Tests for image codex provider."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from core.model_tasks.image_providers import (
    _OPENAI_CODEX_IMAGE_CARRIER_MODEL,
    _OPENAI_CODEX_IMAGE_TOOL_KEYS,
    ProviderImageClient,
    _build_openai_codex_image_payload,
    _parse_openai_codex_image_response,
)
from core.model_tasks.image_types import ImageInput
from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderOutcomeUnknownError,
)
from core.providers.openai import CODEX_EXTRA_HEADERS, CODEX_RESPONSES_MODE
from core.providers.openai_subscription_auth import OPENAI_AUTH_CLAIM
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENAI_CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"


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
                "future_option": "kept",
            },
        },
    )

    tool = payload["tools"][0]
    assert tool == {
        "type": "image_generation",
        "output_format": "webp",
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
