"""Speech request cancellation through the real ASGI receive lifecycle."""

from __future__ import annotations

import asyncio
import json
from collections.abc import MutableMapping
from types import SimpleNamespace
from typing import Any

import pytest

from core.model_tasks import SpeechExecutionError, SpeechSynthesisResult, SpeechTranscriptionResult
from server.app import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["transcribe", "synthesize"])
@pytest.mark.parametrize("ending", ["result", "error", "disconnect", "cancel"])
async def test_plain_speech_settles_on_result_disconnect_or_server_cancellation(
    surface: str,
    ending: str,
) -> None:
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    started = asyncio.Event()
    complete = asyncio.Event()
    cancelled = asyncio.Event()
    release_cleanup = asyncio.Event()
    settled = asyncio.Event()
    waiting_receives = 0
    sent: list[dict[str, Any]] = []

    async def operation(
        value: bytes | str, *, filename: str | None = None, media_type: str | None = None
    ) -> Any:
        if surface == "transcribe":
            assert (value, filename, media_type) == (b"audio", "clip.wav", "audio/wav")
        else:
            assert value == "hello"
        started.set()
        try:
            await complete.wait()
            if ending == "error":
                raise SpeechExecutionError("Test transcription failure")
            if surface == "transcribe":
                return SpeechTranscriptionResult(text="hello")
            return SpeechSynthesisResult(audio=b"audio", media_type="audio/mpeg", format="mp3")
        except asyncio.CancelledError:
            cancelled.set()
            # A local engine may still be settling a running inference.
            await release_cleanup.wait()
            raise
        finally:
            settled.set()

    async def receive() -> dict[str, Any]:
        nonlocal waiting_receives
        waiting_receives += 1
        try:
            return await incoming.get()
        finally:
            waiting_receives -= 1

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(dict(message))

    runtime = SimpleNamespace(
        speech=SimpleNamespace(transcribe=operation, synthesize=operation),
        speech_upload_max_size_bytes=1024,
    )
    app = create_app(runtime=runtime)
    # No other Runtime services or lifespan work participate in this HTTP contract.
    app.state.runtime = runtime
    if surface == "transcribe":
        body = (
            b"--speech-upload\r\n"
            b'Content-Disposition: form-data; name="file"; filename="clip.wav"\r\n'
            b"Content-Type: audio/wav\r\n\r\n"
            b"audio\r\n--speech-upload--\r\n"
        )
        content_type = b"multipart/form-data; boundary=speech-upload"
    else:
        body = b'{"text":"hello"}'
        content_type = b"application/json"
    path = f"/api/speech/{surface}"
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", content_type)],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8420),
    }
    incoming.put_nowait({"type": "http.request", "body": body[:80], "more_body": True})
    incoming.put_nowait({"type": "http.request", "body": body[80:], "more_body": False})
    before = asyncio.all_tasks()
    async with asyncio.timeout(1):
        task = asyncio.create_task(app(scope, receive, send))
        try:
            await started.wait()
            if ending in {"result", "error"}:
                complete.set()
                await task
            else:
                if ending == "disconnect":
                    incoming.put_nowait({"type": "http.disconnect"})
                else:
                    task.cancel()
                await cancelled.wait()
                assert not task.done()
                assert not settled.is_set()
                # A second ASGI cancellation cannot abandon the running engine.
                task.cancel()
                release_cleanup.set()
                if ending == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    await task
        finally:
            release_cleanup.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert settled.is_set()
    assert cancelled.is_set() == (ending in {"disconnect", "cancel"})
    assert waiting_receives == 0
    assert not (asyncio.all_tasks() - before)
    if ending != "cancel":
        assert sent[0]["status"] == {"result": 200, "error": 502, "disconnect": 499}[ending]
        response_body = b"".join(message.get("body", b"") for message in sent)
        if ending == "result":
            if surface == "transcribe":
                assert json.loads(response_body) == {"text": "hello"}
            else:
                assert response_body == b"audio"
