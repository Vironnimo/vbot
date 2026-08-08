"""Wire-contract tests for OpenRouter Video and Music generation."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from core.model_tasks.image_types import ImageInput
from core.model_tasks.music_providers import ProviderMusicClient, _music_payload
from core.model_tasks.video_providers import ProviderVideoClient, _video_payload
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig


def test_video_payload_routes_native_options_and_frame_images() -> None:
    payload = _video_payload(
        "black-forest-labs/flux-3-video",
        "A river at dawn",
        options={
            "duration": "8",
            "resolution": "1080p",
            "generate_audio": True,
            "provider_options": {"google-vertex": {"version": "v2"}},
        },
        frame_images=(("first_frame", ImageInput("start.png", "image/png", b"start")),),
    )

    assert payload["duration"] == 8
    assert payload["resolution"] == "1080p"
    assert payload["generate_audio"] is True
    assert payload["provider"] == {"options": {"google-vertex": {"version": "v2"}}}
    assert payload["frame_images"] == [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(b"start").decode("ascii")
            },
            "frame_type": "first_frame",
        }
    ]


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

    result = await _openrouter_video_client().generate(
        "A river at dawn",
        options={},
        poll_interval=0,
    )

    assert create.call_count == poll.call_count == content.call_count == 1
    assert result.data == b"video-bytes"
    assert result.media_type == "video/mp4"
    assert result.job_id == "job-1"


def test_music_payload_uses_audio_modalities_and_reference_images() -> None:
    payload = _music_payload(
        "google/lyria-3-pro-preview",
        "Dreamy synthwave",
        options={"temperature": 0.7, "seed": 4},
        input_images=(ImageInput("cover.png", "image/png", b"cover"),),
    )

    assert payload["modalities"] == ["text", "audio"]
    assert payload["stream"] is True
    assert payload["temperature"] == 0.7
    assert payload["seed"] == 4
    content = payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Dreamy synthwave"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
@respx.mock
async def test_music_client_concatenates_streamed_base64_audio() -> None:
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

    result = await _openrouter_music_client().generate("Dreamy synthwave", options={})

    assert result.data == b"music-bytes"
    assert result.media_type == "audio/mpeg"
    request = json.loads(route.calls[0].request.content)
    assert request["model"] == "google/lyria-3-pro-preview"
    assert request["stream"] is True


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
