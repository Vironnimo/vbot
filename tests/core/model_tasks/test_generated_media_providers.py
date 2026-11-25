"""Wire-contract tests for OpenRouter and Google Video and Music generation."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.model_tasks.image_types import ImageInput
from core.model_tasks.music_providers import MUSIC_REQUEST_TIMEOUT_SECONDS, ProviderMusicClient
from core.model_tasks.video_providers import ProviderVideoClient, VideoJobUnfinishedError
from core.providers.errors import (
    NetworkError,
    ProviderContentRefusedError,
    ProviderError,
    ProviderOutcomeUnknownError,
    ProviderUsageLimitError,
)
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

    client = _openrouter_video_client()
    result = await client.generate(
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
    # Reconnecting to the same accepted job performs GETs only.
    recovered = await client.collect_existing("job-1", "https://openrouter.ai/api/v1")
    assert recovered is not None and recovered.data == result.data
    assert create.call_count == 1


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
        # A known job whose state is unreadable may still finish and be billed.
        pytest.param(
            {"id": "job-1", "status": "pending"},
            {"id": "job-1", "status": []},
            True,
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
async def test_video_job_survives_transient_poll_failures_and_reports_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _openrouter_video_client()
    monkeypatch.setattr(client, "post_and_parse", AsyncMock(return_value=("job-1", {})))
    poll = AsyncMock(
        side_effect=[
            NetworkError("offline"),
            ProviderError("503 Service Unavailable", retryable=True),
            {
                "status": "failed",
                "error": {"message": "Your request was rejected by the safety system"},
            },
        ]
    )
    monkeypatch.setattr(client, "get_and_parse", poll)

    with pytest.raises(ProviderContentRefusedError) as caught:
        await client.generate("A river", options={}, poll_interval=0)

    assert caught.value.reason == "Your request was rejected by the safety system"
    assert poll.await_count == 3


@pytest.mark.parametrize(
    ("poll", "poll_timeout", "reason"),
    [
        pytest.param(
            AsyncMock(return_value={"status": "pending"}),
            0.05,
            "it had not finished after 1 minutes",
            id="deadline",
        ),
        # A generous deadline: these fail on their own, and a slow worker must not reach it.
        pytest.param(
            AsyncMock(side_effect=ProviderError("404 Not Found", retryable=False)),
            30,
            "checking its status failed",
            id="job-lost",
        ),
        pytest.param(
            AsyncMock(side_effect=[{"status": "completed"}, ProviderError("500", retryable=True)]),
            30,
            "the finished video could not be downloaded",
            id="download-failed",
        ),
    ],
)
@pytest.mark.asyncio
async def test_video_job_whose_result_is_not_collected_names_the_job(
    monkeypatch: pytest.MonkeyPatch, poll: AsyncMock, poll_timeout: float, reason: str
) -> None:
    client = _openrouter_video_client()
    monkeypatch.setattr(client, "post_and_parse", AsyncMock(return_value=("job-1", {})))
    monkeypatch.setattr(client, "get_and_parse", poll)

    with pytest.raises(VideoJobUnfinishedError) as caught:
        await client.generate("A river", options={}, poll_timeout=poll_timeout, poll_interval=0.01)

    assert caught.value.job_id == "job-1"
    assert caught.value.reason.startswith(reason)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "events", "reason"),
    [
        pytest.param(
            200,
            [{"choices": [{"delta": {"content": "I can't create songs about real people."}}]}],
            "I can't create songs about real people.",
            id="text-instead-of-audio",
        ),
        pytest.param(
            200,
            [{"choices": [{"delta": {"content": "Blocked."}, "finish_reason": "content_filter"}]}],
            "Blocked.",
            id="content-filter",
        ),
        pytest.param(
            200,
            # GLM's name for a content-safety block (Z.ai Chat Completion reference).
            [{"choices": [{"delta": {"content": "Blocked."}, "finish_reason": "sensitive"}]}],
            "Blocked.",
            id="sensitive",
        ),
        pytest.param(
            400,
            {"error": {"code": "content_policy_violation", "message": "Not allowed"}},
            "Not allowed",
            id="refused-request",
        ),
    ],
)
@respx.mock
async def test_music_refusals_carry_the_models_reason(
    status: int, events: object, reason: str
) -> None:
    if status == 200:
        assert isinstance(events, list)
        body = "".join(_sse(event) for event in events) + "data: [DONE]\n\n"
        response = httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    else:
        response = httpx.Response(status, json=events)
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(return_value=response)

    with pytest.raises(ProviderContentRefusedError) as caught:
        await _openrouter_music_client().generate("A song", options={"seed": ""})

    assert caught.value.reason == reason


GOOGLE_BASE = "https://generativelanguage.googleapis.com/v1beta"
GOOGLE_MUSIC_URL = f"{GOOGLE_BASE}/models/lyria-3.5:generateContent"


def _google_music_answer(*parts: dict, finish: str | None = "STOP", **fields: object) -> dict:
    candidate: dict = {"content": {"role": "model", "parts": list(parts)}}
    if finish is not None:
        candidate["finishReason"] = finish
    if "finishMessage" in fields:
        candidate["finishMessage"] = fields.pop("finishMessage")
    return {"candidates": [candidate], **fields}


def _google_audio(data: bytes, mime_type: str = "audio/mpeg") -> dict:
    return {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(data).decode()}}


@pytest.mark.asyncio
@respx.mock
async def test_google_music_sends_one_generate_content_and_reads_track_and_lyrics() -> None:
    route = respx.post(GOOGLE_MUSIC_URL).respond(
        200,
        json=_google_music_answer(
            {"text": "[Verse]\nRain on the window"},
            {"text": "draft plan", "thought": True},
            _google_audio(b"RIFF....WAVEfmt ", "audio/wav"),
            {"text": "[Chorus]\nMorning light"},
            usageMetadata={"promptTokenCount": 9, "candidatesTokenCount": 40},
        ),
    )
    observer = AsyncMock()
    observer.start.return_value = "call"
    cover = ImageInput("cover.png", "image/png", b"cover")

    result = await _google_music_client(usage_observer=observer).generate(
        "Rainy lo-fi",
        options={"response_format": "wav", "extra_options": {"labels": {"team": "music"}}},
        input_images=(cover,),
    )

    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == "google-test-key"
    assert json.loads(request.content) == {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": "Rainy lo-fi"},
                    {"inlineData": {"mimeType": "image/png", "data": "Y292ZXI="}},
                ],
            }
        ],
        "generationConfig": {
            "responseModalities": ["AUDIO", "TEXT"],
            "responseFormat": {"audio": {"mimeType": "AUDIO_WAV"}},
        },
        "labels": {"team": "music"},
    }
    assert request.extensions["timeout"]["read"] == MUSIC_REQUEST_TIMEOUT_SECONDS
    assert (result.data, result.media_type, result.model) == (
        b"RIFF....WAVEfmt ",
        "audio/wav",
        "lyria-3.5",
    )
    assert result.text == "[Verse]\nRain on the window\n[Chorus]\nMorning light"
    usage = observer.finish.await_args.kwargs["usage"]
    assert (usage["input_tokens"], usage["output_tokens"]) == (9, 40)


@pytest.mark.parametrize(
    ("response", "outcome", "calls"),
    [
        pytest.param(
            httpx.Response(200, json={"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}),
            "Google blocked the prompt (PROHIBITED_CONTENT)",
            1,
            id="blocked-prompt",
        ),
        pytest.param(
            httpx.Response(
                200,
                json=_google_music_answer(finish="SAFETY", finishMessage="Artist voices."),
            ),
            "Artist voices.",
            1,
            id="safety-finish",
        ),
        pytest.param(
            httpx.Response(200, json=_google_music_answer({"text": "I can't sing that."})),
            "I can't sing that.",
            1,
            id="text-instead-of-audio",
        ),
        # An answer that cannot be confirmed may follow a billed track.
        pytest.param(
            httpx.Response(200, json=_google_music_answer(_google_audio(b"ID3"), finish="OTHER")),
            ProviderOutcomeUnknownError,
            1,
            id="unexpected-finish",
        ),
        pytest.param(
            httpx.Response(200, json=_google_music_answer(_google_audio(b"ID3"), finish=None)),
            ProviderOutcomeUnknownError,
            1,
            id="missing-finish",
        ),
        pytest.param(
            httpx.Response(
                200,
                json=_google_music_answer({"inlineData": {"mimeType": "audio/mpeg", "data": "!"}}),
            ),
            ProviderOutcomeUnknownError,
            1,
            id="undecodable-audio",
        ),
        pytest.param(
            httpx.Response(200, json={"candidates": []}),
            ProviderOutcomeUnknownError,
            1,
            id="no-candidate",
        ),
        pytest.param(httpx.Response(500), ProviderOutcomeUnknownError, 1, id="ambiguous-500"),
        # A zero free-tier quota never succeeds on retry.
        pytest.param(
            httpx.Response(
                429,
                json={
                    "error": {
                        "code": 429,
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "Quota exceeded for metric: x, limit: 0, model: lyria-3-pro",
                    }
                },
            ),
            "quota exhausted",
            1,
            id="zero-quota",
        ),
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_google_music_outcomes_never_replay_a_possibly_billed_track(
    response: httpx.Response, outcome: str | type[Exception], calls: int
) -> None:
    route = respx.post(GOOGLE_MUSIC_URL).mock(return_value=response)

    with pytest.raises(ProviderError) as caught:
        await _google_music_client().generate("A song", options={})

    if isinstance(outcome, str) and isinstance(caught.value, ProviderContentRefusedError):
        assert caught.value.reason == outcome
    elif isinstance(outcome, str):
        assert outcome in str(caught.value) and not caught.value.retryable
    else:
        assert isinstance(caught.value, outcome)
    assert route.call_count == calls


@pytest.mark.parametrize("medium", ["music", "video"])
@pytest.mark.asyncio
@respx.mock
async def test_google_media_retries_documented_overload(medium: str) -> None:
    overloaded = httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE"}})
    client: ProviderMusicClient | ProviderVideoClient
    if medium == "music":
        route = respx.post(GOOGLE_MUSIC_URL).mock(
            side_effect=[
                overloaded,
                httpx.Response(200, json=_google_music_answer(_google_audio(b"ID3track"))),
            ]
        )
        client = _google_music_client()
    else:
        route = respx.post(GOOGLE_INTERACTIONS_URL).mock(
            side_effect=[overloaded, httpx.Response(200, json=_interaction(MP4))]
        )
        client = _google_video_client()

    with patch("core.utils.retry._sleep", new=AsyncMock()):
        result = await client.generate("A song", options={})

    assert (result.data, route.call_count) == (b"ID3track" if medium == "music" else MP4, 2)


GOOGLE_INTERACTIONS_URL = f"{GOOGLE_BASE}/interactions"
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


def _interaction(*content: bytes | dict, status: str = "completed", **fields: object) -> dict:
    items = [
        {
            "type": "video",
            "mime_type": "video/mp4",
            "data": base64.b64encode(item).decode("ascii"),
        }
        if isinstance(item, bytes)
        else item
        for item in content
    ]
    steps = [{"type": "model_output", "content": items}] if items else []
    return {"id": "int-1", "status": status, "steps": steps, **fields}


@pytest.mark.asyncio
@respx.mock
async def test_google_video_runs_a_background_interaction_and_reads_its_inline_video() -> None:
    create = respx.post(GOOGLE_INTERACTIONS_URL).respond(
        200, json={"id": "int-1", "status": "in_progress"}
    )
    poll = respx.get(f"{GOOGLE_INTERACTIONS_URL}/int-1").respond(
        200,
        json=_interaction(
            {"type": "thought", "summary": []},
            MP4,
            usage={
                "total_input_tokens": 1200,
                "total_cached_tokens": 200,
                "total_output_tokens": 5000,
                "total_thought_tokens": 300,
                "total_tokens": 6500,
            },
        ),
    )
    observer = AsyncMock()
    observer.start.return_value = "call"
    start = ImageInput("start.png", "image/png", b"start")
    end = ImageInput("end.png", "image/png", b"end")

    result = await _google_video_client(usage_observer=observer).generate(
        "A river at dawn",
        options={
            "duration": "8",
            "aspect_ratio": "9:16",
            "resolution": "",
            "extra_options": {"labels": {"team": "video"}},
        },
        # Google takes the frames in first, last order whatever order they come in.
        frame_images=(("last_frame", end), ("first_frame", start)),
        poll_interval=0,
    )

    request = create.calls.last.request
    assert request.headers["x-goog-api-key"] == "google-test-key"
    assert json.loads(request.content) == {
        "model": "gemini-omni-1.2-flash",
        "input": [
            {"type": "image", "data": "c3RhcnQ=", "mime_type": "image/png"},
            {"type": "image", "data": "ZW5k", "mime_type": "image/png"},
            {"type": "text", "text": "A river at dawn"},
        ],
        "response_format": {"type": "video", "aspect_ratio": "9:16", "duration": "8s"},
        "generation_config": {"video_config": {"task": "image_to_video"}},
        "background": True,
        "store": True,
        "labels": {"team": "video"},
    }
    assert (create.call_count, poll.call_count) == (1, 1)
    assert (result.data, result.media_type, result.job_id) == (MP4, "video/mp4", "int-1")
    canonical = {
        "input_tokens": 1200,
        "cache_read_tokens": 200,
        "output_tokens": 5300,
        "reasoning_tokens": 300,
    }
    # The poll that finds the finished interaction records its Usage on the create call.
    observer.update_latest.assert_awaited_with(canonical)
    assert result.usage == canonical
    assert result.raw is not None
    assert "data" not in result.raw["steps"][0]["content"][1]


@pytest.mark.parametrize(
    ("finished", "outcome"),
    [
        pytest.param(
            {
                "status": "failed",
                "errors": [
                    {"code": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota."}
                ],
            },
            ProviderUsageLimitError,
            id="quota",
        ),
        pytest.param(
            {"status": "failed", "errors": [{"code": "PROHIBITED_CONTENT", "message": "Blocked."}]},
            "Blocked.",
            id="blocked",
        ),
        pytest.param(
            _interaction({"type": "text", "text": "I can't film that."}),
            "I can't film that.",
            id="text-instead-of-video",
        ),
        pytest.param(
            {"status": "failed", "errors": [{"code": "INTERNAL", "message": "Try later."}]},
            "INTERNAL: Try later.",
            id="failed",
        ),
        pytest.param({"status": "requires_action"}, "requires_action", id="awaits-input"),
        pytest.param(_interaction(), "without a video", id="no-video"),
        # A video vBot cannot read is still stored at Google under the job id.
        pytest.param(
            _interaction({"type": "video", "uri": "https://example.invalid/v.mp4"}),
            VideoJobUnfinishedError,
            id="video-without-data",
        ),
        pytest.param(
            _interaction({"type": "video", "data": "!"}),
            VideoJobUnfinishedError,
            id="undecodable-video",
        ),
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_google_video_outcomes_never_resubmit_the_interaction(
    finished: dict[str, object], outcome: str | type[Exception]
) -> None:
    create = respx.post(GOOGLE_INTERACTIONS_URL).respond(
        200, json={"id": "int-1", "status": "in_progress"}
    )
    respx.get(f"{GOOGLE_INTERACTIONS_URL}/int-1").respond(200, json=finished)

    terminal = AsyncMock()
    with pytest.raises(ProviderError) as caught:
        await _google_video_client().generate(
            "A river", options={}, poll_interval=0, ended_without_video=terminal
        )
    if isinstance(caught.value, VideoJobUnfinishedError):
        terminal.assert_not_awaited()
    else:
        terminal.assert_awaited_once()

    if isinstance(outcome, type):
        assert isinstance(caught.value, outcome)
    elif isinstance(caught.value, ProviderContentRefusedError):
        assert caught.value.reason == outcome
    else:
        assert outcome in str(caught.value) and not caught.value.retryable
    assert create.call_count == 1


@pytest.mark.parametrize(
    ("options", "frames"),
    [
        pytest.param({}, ("last_frame",), id="last-frame-alone"),
        pytest.param({"duration": "12"}, ("first_frame", "last_frame"), id="long-interpolation"),
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_google_video_rejects_frames_omni_cannot_place_before_sending(
    options: dict[str, object], frames: tuple[str, ...]
) -> None:
    route = respx.post(GOOGLE_INTERACTIONS_URL).respond(200, json={"id": "int-1"})
    image = ImageInput("frame.png", "image/png", b"frame")

    with pytest.raises(ProviderError) as caught:
        await _google_video_client().generate(
            "A river", options=options, frame_images=tuple((name, image) for name in frames)
        )

    assert not caught.value.retryable
    assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_cancelled_google_video_cancels_the_interaction_and_follows_it() -> None:
    respx.post(GOOGLE_INTERACTIONS_URL).respond(200, json={"id": "int-1", "status": "in_progress"})
    cancel = respx.post(f"{GOOGLE_INTERACTIONS_URL}/int-1/cancel").respond(
        200, json={"id": "int-1", "status": "cancelled"}
    )
    polled = asyncio.Event()

    def status(_request: httpx.Request) -> httpx.Response:
        polled.set()
        state = "cancelled" if cancel.called else "in_progress"
        return httpx.Response(200, json={"id": "int-1", "status": state})

    respx.get(f"{GOOGLE_INTERACTIONS_URL}/int-1").mock(side_effect=status)
    handed: list[tuple[str, asyncio.Task]] = []
    generation = asyncio.create_task(
        _google_video_client().generate(
            "A river",
            options={},
            poll_interval=0.01,
            on_cancelled_job=lambda job_id, task: handed.append((job_id, task)),
        )
    )
    await asyncio.wait_for(polled.wait(), timeout=1)
    generation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await generation

    [(job_id, follower)] = handed
    with pytest.raises(ProviderError, match="cancelled"):
        await asyncio.wait_for(follower, timeout=1)
    assert (job_id, cancel.call_count) == ("int-1", 1)


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


def _google_music_client(*, usage_observer: object = None) -> ProviderMusicClient:
    provider = ProviderConfig(
        id="google",
        name="Google AI Studio",
        adapter="google",
        base_url=GOOGLE_BASE,
        connections=[],
    )
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(header="x-goog-api-key", prefix="", credential_key="GEMINI_API_KEY"),
    )
    return ProviderMusicClient(
        provider=provider,
        connection=connection,
        credential="google-test-key",
        model_id="lyria-3.5",
        usage_observer=usage_observer,  # type: ignore[arg-type]
    )


def _google_video_client(*, usage_observer: object = None) -> ProviderVideoClient:
    provider = ProviderConfig(
        id="google",
        name="Google AI Studio",
        adapter="google",
        base_url=GOOGLE_BASE,
        connections=[],
    )
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(header="x-goog-api-key", prefix="", credential_key="GEMINI_API_KEY"),
    )
    return ProviderVideoClient(
        provider=provider,
        connection=connection,
        credential="google-test-key",
        model_id="gemini-omni-1.2-flash",
        usage_observer=usage_observer,  # type: ignore[arg-type]
    )
