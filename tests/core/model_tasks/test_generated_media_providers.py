"""Wire-contract tests for OpenRouter Video and Music generation."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from core.model_tasks.image_types import ImageInput
from core.model_tasks.music_providers import MUSIC_REQUEST_TIMEOUT_SECONDS, ProviderMusicClient
from core.model_tasks.video_providers import ProviderVideoClient
from core.providers.errors import ProviderError, ProviderOutcomeUnknownError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig


def _data_url(image: ImageInput) -> str:
    return f"data:{image.media_type};base64,{base64.b64encode(image.data).decode('ascii')}"


@pytest.mark.asyncio
@respx.mock
async def test_video_client_submits_polls_and_downloads_same_origin_content() -> None:
    create = respx.post("https://openrouter.ai/api/v1/videos").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": "job-1",
                "polling_url": "https://attacker.invalid/steal",
                "status": "pending",
            },
        )
    )
    poll = respx.get("https://openrouter.ai/api/v1/videos/job-1").mock(
        return_value=httpx.Response(
            200,
            json={"id": "job-1", "polling_url": "/videos/job-1", "status": "completed"},
        )
    )
    content = respx.get("https://openrouter.ai/api/v1/videos/job-1/content?index=0").mock(
        return_value=httpx.Response(
            200,
            content=b"video-bytes",
            headers={"content-type": "video/mp4"},
        )
    )
    start = ImageInput("start.png", "image/png", b"start")

    result = await _openrouter_video_client().generate(
        "A river at dawn",
        options={
            "duration": "8",
            "resolution": "1080p",
            "generate_audio": True,
            "provider_options": {"google-vertex": {"version": "v2"}},
        },
        frame_images=(("first_frame", start),),
        poll_interval=0,
    )

    assert json.loads(create.calls.last.request.content) == {
        "model": "black-forest-labs/flux-3-video",
        "prompt": "A river at dawn",
        "duration": 8,
        "resolution": "1080p",
        "generate_audio": True,
        "provider": {"options": {"google-vertex": {"version": "v2"}}},
        "frame_images": [
            {
                "type": "image_url",
                "image_url": {"url": _data_url(start)},
                "frame_type": "first_frame",
            }
        ],
    }
    # The advertised polling URL is ignored in favor of the same-origin job path.
    assert create.call_count == poll.call_count == content.call_count == 1
    assert (result.data, result.media_type, result.job_id) == (b"video-bytes", "video/mp4", "job-1")


@pytest.mark.parametrize("medium", ["video", "music"])
@pytest.mark.asyncio
@respx.mock
async def test_extra_options_cannot_redirect_the_model(medium: str) -> None:
    route = respx.post(url__regex=r"https://openrouter\.ai/.*").respond(500)
    client = _openrouter_video_client() if medium == "video" else _openrouter_music_client()

    with pytest.raises(ProviderError, match="model"):
        await client.generate("prompt", options={"extra_options": {"model": "redirected/model"}})

    assert route.call_count == 0


@pytest.mark.parametrize(
    ("created", "polled", "outcome_unknown"),
    [
        # A submitted job without a usable id may already be running and billed.
        pytest.param({"id": None, "status": "pending"}, None, True, id="missing-job-id"),
        pytest.param({"id": "", "status": "pending"}, None, True, id="empty-job-id"),
        pytest.param({"id": "job-1", "status": 123}, None, True, id="malformed-created-status"),
        # A known job fails without being submitted again.
        pytest.param(
            {"id": "job-1", "status": "pending"},
            {"id": "job-1", "status": []},
            False,
            id="malformed-polled-status",
        ),
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_malformed_video_job_fails_without_resubmission(
    created: dict[str, object], polled: dict[str, object] | None, outcome_unknown: bool
) -> None:
    create = respx.post("https://openrouter.ai/api/v1/videos").respond(202, json=created)
    poll = respx.get("https://openrouter.ai/api/v1/videos/job-1").respond(200, json=polled)

    with pytest.raises(ProviderError) as caught:
        await _openrouter_video_client().generate("A river at dawn", options={}, poll_interval=0)

    assert isinstance(caught.value, ProviderOutcomeUnknownError) is outcome_unknown
    assert caught.value.retryable is False
    assert (create.call_count, poll.call_count) == (1, 0 if polled is None else 1)


@pytest.mark.asyncio
async def test_video_poll_deadline_includes_poll_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _openrouter_video_client()
    monkeypatch.setattr(
        client, "post_and_parse", AsyncMock(return_value=("job-1", {"status": "pending"}))
    )
    poll = AsyncMock(return_value={"status": "completed"})
    monkeypatch.setattr(client, "get_and_parse", poll)

    with pytest.raises(ProviderError) as caught:
        await client.generate("A river", options={}, poll_timeout=0.01, poll_interval=1)

    assert caught.value.retryable is False
    poll.assert_not_awaited()


@pytest.mark.asyncio
async def test_video_poll_deadline_cancels_stalled_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _openrouter_video_client()
    monkeypatch.setattr(
        client, "post_and_parse", AsyncMock(return_value=("job-1", {"status": "pending"}))
    )
    closed = asyncio.Event()

    async def stalled_poll(*args: object, **kwargs: object) -> None:
        try:
            await asyncio.Future()
        finally:
            closed.set()

    monkeypatch.setattr(client, "get_and_parse", stalled_poll)
    with pytest.raises(ProviderError):
        await asyncio.wait_for(
            client.generate("A river", options={}, poll_timeout=0.01, poll_interval=0),
            timeout=1,
        )

    assert closed.is_set()


@pytest.mark.asyncio
@respx.mock
async def test_music_client_streams_audio_and_concatenates_the_base64_chunks() -> None:
    encoded = base64.b64encode(b"music-bytes").decode("ascii")
    stream = "".join(
        (
            _sse({"choices": [{"delta": {"audio": {"data": encoded[:5]}}}]}),
            _sse({"choices": [{"delta": {"audio": {"data": encoded[5:]}}}]}),
            "data: [DONE]\n\n",
        )
    )
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            text=stream,
            headers={"content-type": "text/event-stream"},
        )
    )
    cover = ImageInput("cover.png", "image/png", b"cover")

    result = await _openrouter_music_client().generate(
        "Dreamy synthwave",
        options={"temperature": 0.7, "seed": 4},
        input_images=(cover,),
    )

    assert (result.data, result.media_type) == (b"music-bytes", "audio/mpeg")
    request = route.calls.last.request
    assert json.loads(request.content) == {
        "model": "google/lyria-3-pro-preview",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Dreamy synthwave"},
                    {"type": "image_url", "image_url": {"url": _data_url(cover)}},
                ],
            }
        ],
        "modalities": ["text", "audio"],
        "stream": True,
        "temperature": 0.7,
        "seed": 4,
    }
    assert request.extensions["timeout"]["read"] == MUSIC_REQUEST_TIMEOUT_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [{"message": "generation failed", "code": 502}, "failed"])
@respx.mock
async def test_music_client_rejects_error_after_partial_audio(error: object) -> None:
    stream = (
        _sse({"choices": [{"delta": {"audio": {"data": "YWJj"}}}]})
        + _sse({"error": error})
        + "data: [DONE]\n\n"
    )
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").respond(
        200, text=stream, headers={"content-type": "text/event-stream"}
    )

    with pytest.raises(ProviderError):
        await _openrouter_music_client().generate("Dreamy synthwave", options={})

    assert route.call_count == 1


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _openrouter_provider() -> ProviderConfig:
    return ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url="https://openrouter.ai/api/v1",
        connections=[],
        extra_headers={"X-Title": "vBot"},
    )


def _connection() -> ConnectionConfig:
    return ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="OPENROUTER_API_KEY",
        ),
    )


def _openrouter_video_client() -> ProviderVideoClient:
    return ProviderVideoClient(
        provider=_openrouter_provider(),
        connection=_connection(),
        credential="sk-test",
        model_id="black-forest-labs/flux-3-video",
    )


def _openrouter_music_client() -> ProviderMusicClient:
    return ProviderMusicClient(
        provider=_openrouter_provider(),
        connection=_connection(),
        credential="sk-test",
        model_id="google/lyria-3-pro-preview",
    )
