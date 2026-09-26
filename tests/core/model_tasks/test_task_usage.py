"""Task consumption is recorded at execution, independently of result retention."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from core.model_tasks.decisions import DecisionService
from core.model_tasks.embeddings import EmbeddingService
from core.model_tasks.embeddings_providers import _parse_embedding_usage
from core.model_tasks.image import ImageService
from core.model_tasks.model_tasks import TaskModelService, parse_task_model_target_id
from core.model_tasks.music import MusicService
from core.model_tasks.speech import SpeechService
from core.model_tasks.task_execution import TaskUsage, TaskUsageContext, task_usage
from core.model_tasks.video import VideoService
from core.model_tasks.video_providers import ProviderVideoClient
from core.providers.errors import ProviderError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.task_client import ProviderTaskClient
from core.providers.token_getter import StaticTokenGetter
from core.usage import UsageRecorder

BASE = "https://provider.example/api/v1"
TASKS = (
    "text_embedding",
    "image_generation",
    "video_generation",
    "music_generation",
    "speech_to_text",
    "text_to_speech",
    "decision",
)
SCOPE = TaskUsageContext(
    agent_id="agent",
    project_id="project",
    session_id="session",
    run_id="run",
    owner_name="extension",
    group_id="group",
)


def _runtime() -> Any:
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(header="Authorization", prefix="Bearer ", credential_key="TEST_KEY"),
    )
    provider = ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url=BASE,
        connections=[connection],
    )
    return SimpleNamespace(
        providers=SimpleNamespace(get=lambda _identifier: provider),
        get_connection_token_getter=lambda _connection: StaticTokenGetter("test-token"),
    )


class _Bindings:
    def binding_for(self, task_type: str) -> Any:
        return SimpleNamespace(
            task_type=task_type, target="openrouter/test/model::api-key", options={}
        )

    def binding_is_usable(self, _kind: str) -> bool:
        return True

    def validate_execution_target(self, _binding: Any) -> None:
        pass

    def options_with_defaults(self, _binding: Any) -> dict[str, Any]:
        return {}

    def model_for_target(self, _target: Any) -> Any:
        return SimpleNamespace(
            capabilities=SimpleNamespace(
                task_types=TASKS,
                task_options={},
                input_modalities=("text",),
            )
        )


def _audio() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 160)
    return stream.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", TASKS)
@respx.mock
async def test_task_services_record_consumption_before_artifacts_or_experiment_history(
    kind: str,
    recorder: UsageRecorder,
    tmp_path: Path,
) -> None:
    usage = {"input_tokens": 3, "output_tokens": 2, "cost": 0.04}
    tasks, runtime = _Bindings(), _runtime()
    kwargs: dict[str, Any] = {"usage_recorder": recorder}
    if kind == "text_embedding":
        respx.post(BASE + "/embeddings").respond(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.1]}],
                "usage": {"prompt_tokens": 3, "cost": 0.04},
            },
        )
        await EmbeddingService(tasks, runtime, **kwargs).embed(["text"])
    elif kind == "image_generation":
        respx.post(BASE + "/images").respond(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(b"image").decode()}],
                "usage": usage,
            },
        )
        await ImageService(tasks, runtime, **kwargs).generate("image", usage_context=SCOPE)
    elif kind == "video_generation":
        respx.post(BASE + "/videos").respond(
            200, json={"id": "job", "status": "completed", "usage": usage}
        )
        respx.get(BASE + "/videos/job/content?index=0").respond(200, content=b"video")
        await VideoService(tasks, runtime, **kwargs).generate("video", usage_context=SCOPE)
    elif kind == "music_generation":
        body = (
            "data: "
            + json.dumps(
                {
                    "choices": [{"delta": {"audio": {"data": "YWJj"}}}],
                    "usage": usage,
                }
            )
            + "\n\ndata: [DONE]\n\n"
        )
        respx.post(BASE + "/chat/completions").respond(200, content=body.encode())
        await MusicService(tasks, runtime, **kwargs).generate("music", usage_context=SCOPE)
    elif kind in {"speech_to_text", "text_to_speech"}:
        service = SpeechService(tasks, runtime, tmp_path, **kwargs)
        try:
            if kind == "speech_to_text":
                respx.post(BASE + "/audio/transcriptions").respond(
                    200, json={"text": "hello", "usage": usage}
                )
                await service.transcribe(_audio())
            else:
                respx.post(BASE + "/audio/speech").respond(200, content=b"audio")
                await service.synthesize("hello", usage_context=SCOPE)
        finally:
            await service.aclose()
    else:
        decisions = DecisionService(
            cast(TaskModelService, tasks), runtime, tmp_path / "decisions.db", **kwargs
        )
        try:
            respx.post("https://provider.example/api/alpha/decisions").respond(
                200,
                json={
                    "model": "test/model",
                    "answers": {"q": {"type": "noul", "noul": 0.5}},
                    "usage": usage,
                },
            )
            await decisions.evaluate(
                "state", [{"id": "q", "type": "noul", "instructions": "Judge"}], usage_context=SCOPE
            )
        finally:
            await decisions.aclose()

    _, records = recorder.read_since()
    assert len(records) == 1
    record = records[0]
    assert (record.model, record.kind, record.status) == (
        "openrouter/test/model",
        kind,
        "completed",
    )
    if kind not in {"text_embedding", "speech_to_text"}:
        assert (record.agent_id, record.project_id, record.session_id, record.run_id) == (
            "agent",
            "project",
            "session",
            "run",
        )
        assert (record.owner_name, record.group_id) == ("extension", "group")
    if kind == "text_to_speech":
        assert "input_tokens" not in record.usage and "reported_cost_usd" not in record.usage
    else:
        assert record.usage["input_tokens"] == 3
        assert record.usage["reported_cost_usd"] == 0.04
    if kind == "text_embedding":
        assert record.usage["output_tokens"] == 0


@pytest.mark.asyncio
@respx.mock
async def test_retry_and_rejected_result_keep_separate_usage_records(
    recorder: UsageRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.utils.retry._sleep", AsyncMock())
    target = parse_task_model_target_id("openrouter/test/model::api-key")
    observer = TaskUsage(recorder, "text_embedding", target)
    client = ProviderTaskClient.from_runtime(_runtime(), target, usage_observer=observer)
    route = respx.post(BASE + "/task").mock(
        side_effect=[
            httpx.Response(200, json={"usage": {"input_tokens": 4}}),
            httpx.Response(200, json={"usage": {"input_tokens": 6}}),
        ]
    )

    def parse(response: httpx.Response) -> dict[str, Any]:
        if len(route.calls) == 1:
            raise ProviderError("retryable unusable result", retryable=True)
        return dict(response.json())

    await client.post_and_parse("/task", timeout=1, parse=parse, json={})
    _, records = recorder.read_since()
    assert [(record.status, record.usage["input_tokens"]) for record in records] == [
        ("failed", 4),
        ("completed", 6),
    ]


@pytest.mark.asyncio
@respx.mock
async def test_video_poll_updates_create_usage_before_download_failure(
    recorder: UsageRecorder,
) -> None:
    target = parse_task_model_target_id("openrouter/test/model::api-key")
    client = ProviderVideoClient.from_runtime(
        _runtime(), target, usage_observer=TaskUsage(recorder, "video_generation", target)
    )
    respx.post(BASE + "/videos").respond(200, json={"id": "job", "status": "pending"})
    respx.get(BASE + "/videos/job").respond(
        200, json={"status": "completed", "usage": {"cost": 0.4}}
    )
    respx.get(BASE + "/videos/job/content?index=0").respond(404)
    with pytest.raises(ProviderError):
        await client.generate("video", options={}, poll_interval=0)
    _, records = recorder.read_since()
    assert len(records) == 1
    assert records[0].usage["reported_cost_usd"] == 0.4


@pytest.mark.asyncio
async def test_local_cancelled_attempt_stays_unknown_and_cost_projection_keeps_snapshot(
    recorder: UsageRecorder,
) -> None:
    observer = TaskUsage(recorder, "text_to_speech", parse_task_model_target_id("local/tts"))
    with pytest.raises(asyncio.CancelledError):
        async with observer.attempt():
            raise asyncio.CancelledError
    _, records = recorder.read_since()
    assert len(records) == 1
    assert records[0].model == "local/tts" and records[0].status == "cancelled"
    assert "input_tokens" not in records[0].usage
    assert task_usage({"cost": {"total_usd": 1}})["cost"] == {"total_usd": 1}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, {}),
        ({"prompt_tokens": 0}, {"input_tokens": 0, "output_tokens": 0}),
        ({"input_tokens": 7}, {"input_tokens": 7, "output_tokens": 0}),
        ({"total_tokens": 7}, {"output_tokens": 0}),
        ({"cost": "0.25"}, {"reported_cost_usd": 0.25}),
        ({"cost": 0}, {"reported_cost_usd": 0.0}),
        ({"prompt_tokens": True, "cost": -1}, {}),
    ],
)
def test_embedding_usage_preserves_report_provenance(
    raw: Any,
    expected: dict[str, Any],
) -> None:
    assert task_usage(_parse_embedding_usage(raw)) == expected


@pytest.mark.asyncio
async def test_embedding_result_preserves_normalized_cost_without_raw_usage(
    recorder: UsageRecorder,
) -> None:
    observer = TaskUsage(
        recorder, "text_embedding", parse_task_model_target_id("openrouter/test/model::api-key")
    )
    call_id = await observer.start()
    await observer.finish(
        call_id,
        result=SimpleNamespace(usage=_parse_embedding_usage({"input_tokens": 9, "cost": "0.2"})),
    )
    _, records = recorder.read_since()
    assert len(records) == 1
    assert records[0].usage["input_tokens"] == 9
    assert records[0].usage["output_tokens"] == 0
    assert records[0].usage["reported_cost_usd"] == 0.2
