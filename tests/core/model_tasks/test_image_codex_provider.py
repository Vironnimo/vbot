"""Tests for image codex provider."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import respx

from core.model_tasks.image_providers import (
    _OPENAI_CODEX_IMAGE_CARRIER_MODEL,
    ProviderImageClient,
)
from core.model_tasks.image_types import ImageInput
from core.model_tasks.model_tasks import parse_task_model_target_id
from core.model_tasks.task_execution import TaskUsage
from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderOutcomeUnknownError,
)
from core.providers.openai import CODEX_EXTRA_HEADERS, CODEX_RESPONSES_MODE
from core.providers.openai_subscription_auth import OPENAI_AUTH_CLAIM
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.usage import UsageRecorder

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
    usage_observer: TaskUsage | None = None,
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
        usage_observer=usage_observer,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_image", [True, False])
@respx.mock
async def test_subscription_image_records_independent_carrier_even_for_unusable_image(
    recorder: UsageRecorder,
    valid_image: bool,
) -> None:
    target = parse_task_model_target_id("openai/gpt-image-2::subscription")
    client = _openai_subscription_image_client(
        "gpt-image-2",
        usage_observer=TaskUsage(recorder, "image_generation", target),
    )
    body = _openai_codex_image_sse()
    if not valid_image:
        body = body.replace(base64.b64encode(b"codex-image").decode("ascii"), "invalid-base64")
    respx.post(OPENAI_CODEX_RESPONSES_URL).respond(200, content=body.encode())
    if valid_image:
        await client.generate("image", options={})
    else:
        with pytest.raises(ProviderError):
            await client.generate("image", options={})
    _, records = recorder.read_since()
    by_model = {record.model: record for record in records}
    assert len(records) == 2
    image_usage = by_model["openai/gpt-image-2"]
    carrier_usage = by_model[f"openai/{_OPENAI_CODEX_IMAGE_CARRIER_MODEL}"]
    assert (image_usage.usage["input_tokens"], image_usage.usage["output_tokens"]) == (12, 456)
    assert image_usage.status == ("completed" if valid_image else "failed")
    assert (carrier_usage.usage["input_tokens"], carrier_usage.usage["output_tokens"]) == (3, 1)
    assert all(record.kind == "image_generation" for record in records)


def _data_url(image: ImageInput) -> str:
    return f"data:{image.media_type};base64,{base64.b64encode(image.data).decode('ascii')}"


_SOURCES = (
    ImageInput(filename="square.jpg", media_type="image/jpeg", data=b"source-one"),
    ImageInput(filename="style.png", media_type="image/png", data=b"source-two"),
)


@pytest.mark.parametrize(
    ("options", "input_images", "tool", "text", "media_type"),
    [
        # Size, quality and background also reach the carrier as prompt text.
        pytest.param(
            {
                "size": "1024x1536",
                "quality": "low",
                "background": "opaque",
                "moderation": "low",
                "output_format": "webp",
                "output_compression": 50,
            },
            (),
            {
                "output_format": "webp",
                "output_compression": 50,
                "moderation": "low",
                "background": "opaque",
                "size": "1024x1536",
                "quality": "low",
            },
            "Use the image_generation tool to render "
            "(size 1024x1536, quality low, background opaque): a cat",
            "image/webp",
            id="every-tool-option",
        ),
        # The image tool rejects ``n`` and overrides ``model``, even from extra_options.
        pytest.param(
            {
                "n": 2,
                "model": "gpt-image-1",
                "output_format": "webp",
                "extra_options": {"n": 10, "model": "gpt-image-2", "future_option": "kept"},
            },
            (),
            {"output_format": "webp", "future_option": "kept"},
            "Use the image_generation tool to render: a cat",
            "image/webp",
            id="n-and-model-never-forwarded",
        ),
        # Without a requested format the final image call names it.
        pytest.param(
            {},
            _SOURCES,
            {},
            "Use the image_generation tool to edit the provided image(s): a cat",
            "image/jpeg",
            id="source-images-for-editing",
        ),
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_subscription_image_asks_a_codex_carrier_to_call_the_image_tool(
    options: dict[str, Any],
    input_images: tuple[ImageInput, ...],
    tool: dict[str, Any],
    text: str,
    media_type: str,
) -> None:
    route = respx.post(OPENAI_CODEX_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            text=_openai_codex_image_sse(b"codex-image", output_format="jpeg"),
            headers={"content-type": "text/event-stream"},
        )
    )

    result = await _openai_subscription_image_client("gpt-image-2").generate(
        "a cat", options=options, input_images=input_images
    )

    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {_openai_subscription_access_token()}"
    assert request.headers["chatgpt-account-id"] == "account-123"
    assert all(request.headers[name] == value for name, value in CODEX_EXTRA_HEADERS.items())
    payload = json.loads(request.content)
    assert {key: payload[key] for key in ("model", "stream", "store", "instructions")} == {
        "model": _OPENAI_CODEX_IMAGE_CARRIER_MODEL,
        "stream": True,
        "store": False,
        "instructions": "You are an image generation assistant.",
    }
    assert payload["tools"] == [{"type": "image_generation", **tool}]
    assert payload["input"][0]["content"] == [
        {"type": "input_text", "text": text},
        *({"type": "input_image", "image_url": _data_url(image)} for image in input_images),
    ]
    # Progressive previews are ignored; the final call and both usages are kept.
    assert (result.images, result.media_type, result.model) == (
        (b"codex-image",),
        media_type,
        "gpt-image-2",
    )
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
    assert result.raw["image_generation_calls"][0]["revised_prompt"] == "a revised image prompt"


@pytest.mark.asyncio
async def test_openai_subscription_image_generate_requires_account_header() -> None:
    client = _openai_subscription_image_client("gpt-image-2", credential="not-a-jwt")

    with pytest.raises(ProviderAuthError, match="reconnect"):
        await client.generate("a cat", options={})


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
