"""Speech HTTP routes, disconnect cancellation and request-local media streams."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, cast, override

from core.model_tasks import (
    SpeechArtifactNotFoundError,
    SpeechConfigurationError,
    SpeechError,
    SpeechExecutionError,
    SpeechUnsupportedTargetError,
)
from core.model_tasks.speech_input import SpeechInputError
from core.model_tasks.speech_playback import PLAYBACK_MEDIA_TYPE, SpeechPlayback
from core.model_tasks.speech_types import SpeechAudioChunk, SpeechBusyError, SpeechProgress
from core.utils.workers import finish_despite_cancel
from server._http_dependencies import (
    FastAPIType,
    FileResponse,
    HTTPException,
    Request,
    Response,
    StreamingResponse,
)
from server._request_bodies import (
    _parse_upload_file_with_limit,
    _read_json_payload_with_limit,
    _read_upload_file_with_limit,
    _require_json_media_type,
)

JsonObject = dict[str, Any]

# How often a speech progress stream reports while the operation still runs.
SPEECH_PROGRESS_HEARTBEAT_SECONDS = 0.5


class _SpeechStreamingResponse(StreamingResponse):  # type: ignore[misc]
    """Close speech generators even when the connection ends during a send."""

    def __init__(self, content: AsyncGenerator[str | bytes], **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._speech_stream = content

    @override
    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # StreamingResponse does not close an iterator suspended at yield.
            # Its request-local inference or spool reader must settle now.
            await finish_despite_cancel(self._speech_stream.aclose())


def register_speech_routes(app: FastAPIType) -> None:
    @app.post("/api/speech/transcribe")
    async def transcribe_speech(request: Request) -> Any:
        runtime = request.app.state.runtime
        speech_service = runtime.speech
        file = await _parse_upload_file_with_limit(
            request,
            max_size_bytes=runtime.speech_upload_max_size_bytes,
            upload_kind="Speech audio",
        )
        filename = file.filename or "recording.webm"
        media_type = file.content_type or "application/octet-stream"
        try:
            audio = await _read_upload_file_with_limit(
                file,
                max_size_bytes=runtime.speech_upload_max_size_bytes,
                upload_kind="Speech audio",
            )
        finally:
            await file.close()
        if "application/x-ndjson" in request.headers.get("accept", ""):
            return _SpeechStreamingResponse(
                _stream_speech(
                    lambda progress: speech_service.transcribe(
                        audio, filename=filename, media_type=media_type, progress=progress
                    )
                ),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        try:
            result = await _speech_until_disconnect(
                request,
                speech_service.transcribe(audio, filename=filename, media_type=media_type),
            )
        except SpeechError as exc:
            raise _speech_http_exception(exc) from exc
        return cast(JsonObject, result.to_dict())

    @app.post("/api/speech/synthesize")
    async def synthesize_speech(request: Request) -> Response:
        _require_json_media_type(request)
        speech_service = request.app.state.runtime.speech
        try:
            payload = await _read_json_payload_with_limit(request)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Request body must be valid JSON",
            ) from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="text must be a non-empty string")
        if "application/x-ndjson" in request.headers.get("accept", ""):
            return _SpeechStreamingResponse(
                _stream_synthesis(speech_service, text),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        try:
            result = await _speech_until_disconnect(request, speech_service.synthesize(text))
        except SpeechError as exc:
            raise _speech_http_exception(exc) from exc
        return Response(content=result.audio, media_type=result.media_type)

    @app.get("/api/speech/playback/{playback_id}")
    async def get_speech_playback(request: Request, playback_id: str) -> StreamingResponse:
        playback = request.app.state.runtime.speech.playbacks.get(playback_id)
        if playback is None:
            raise HTTPException(status_code=410, detail="Speech playback is no longer available")
        return _SpeechStreamingResponse(
            playback.frames(),
            media_type=PLAYBACK_MEDIA_TYPE,
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/speech/artifacts/{artifact_id}")
    async def get_speech_artifact(request: Request, artifact_id: str) -> FileResponse:
        speech_service = request.app.state.runtime.speech
        try:
            artifact = speech_service.get_artifact(artifact_id)
        except SpeechError as exc:
            raise _speech_http_exception(exc) from exc
        return FileResponse(
            artifact.file_path,
            media_type=artifact.media_type,
            filename=artifact.filename,
        )


async def _speech_until_disconnect[Result](
    request: Request, operation: Awaitable[Result]
) -> Result:
    """Run speech after the upload, cancelling and settling it when the client leaves."""

    async def disconnected() -> None:
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    task = asyncio.ensure_future(operation)
    disconnect = asyncio.create_task(disconnected(), name="speech-disconnect")
    try:
        done, _pending = await asyncio.wait({task, disconnect}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        disconnect.result()
        # Nothing reaches the departed client; finish the request without an
        # unhandled cancellation or an internal-error log.
        raise HTTPException(status_code=499, detail="Client disconnected")
    finally:
        if not task.done():
            task.cancel()
        disconnect.cancel()
        await finish_despite_cancel(asyncio.gather(task, disconnect, return_exceptions=True))


async def _stream_speech(
    operation: Callable[[SpeechProgress], Awaitable[Any]],
    *,
    updates: asyncio.Queue[JsonObject] | None = None,
) -> AsyncGenerator[str]:
    """Request-local progress heartbeats followed by one terminal result."""
    progress = SpeechProgress()
    task = asyncio.ensure_future(operation(progress))
    update_task: asyncio.Task[JsonObject] | None = None
    try:
        while not task.done():
            yield json.dumps({"type": "progress", **progress.snapshot()}) + "\n"
            if updates is None:
                await asyncio.wait({task}, timeout=SPEECH_PROGRESS_HEARTBEAT_SECONDS)
            else:
                if update_task is None:
                    update_task = asyncio.create_task(updates.get(), name="speech-playback-ready")
                await asyncio.wait(
                    {task, update_task},
                    timeout=SPEECH_PROGRESS_HEARTBEAT_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if update_task.done():
                    yield json.dumps(update_task.result()) + "\n"
                    update_task = None
        if updates is not None:
            while not updates.empty():
                yield json.dumps(updates.get_nowait()) + "\n"
        try:
            result = task.result()
        except SpeechError as exc:
            error = _speech_http_exception(exc)
            yield (
                json.dumps({"type": "error", "detail": error.detail, "status": error.status_code})
                + "\n"
            )
        except Exception:
            logging.getLogger("vbot.server.app").exception("Speech stream failed")
            yield (
                json.dumps({"type": "error", "detail": "Speech request failed", "status": 500})
                + "\n"
            )
        else:
            yield json.dumps({"type": "result", "result": result.to_dict()}) + "\n"
    finally:
        if not task.done():
            task.cancel()
        if update_task is not None:
            update_task.cancel()
        # Local inference keeps its existing cancellation-safe worker semantics:
        # a disconnected client cannot release an engine still doing work.
        await finish_despite_cancel(
            asyncio.gather(
                task, *([update_task] if update_task is not None else []), return_exceptions=True
            )
        )


def _stream_synthesis(speech_service: Any, text: str) -> AsyncGenerator[str]:
    """Announce early binary playback, then retain the ordinary artifact result."""
    updates: asyncio.Queue[JsonObject] = asyncio.Queue(maxsize=1)
    playback: SpeechPlayback | None = None

    async def publish_audio(chunk: SpeechAudioChunk) -> None:
        nonlocal playback
        first = playback is None
        if first:
            playback = speech_service.playbacks.create()
        if playback is None:
            return
        await playback.append(chunk.audio, chunk.sample_rate_hz)
        if first:
            updates.put_nowait({"type": "playback", "url": playback.url})

    async def synthesize(progress: SpeechProgress) -> Any:
        try:
            result = await speech_service.synthesize_artifact(
                text, progress=progress, on_audio=publish_audio
            )
        except BaseException as exc:
            if playback is not None:
                playback.finish(
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                )
            raise
        else:
            if playback is not None:
                playback.finish()
            return result

    return _stream_speech(synthesize, updates=updates)


def _speech_http_exception(error: SpeechError) -> HTTPException:
    if isinstance(error, SpeechArtifactNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, SpeechBusyError):
        return HTTPException(status_code=429, detail=str(error))
    if isinstance(error, SpeechInputError):
        return HTTPException(status_code=413 if error.too_large else 400, detail=str(error))
    if isinstance(error, SpeechConfigurationError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, SpeechUnsupportedTargetError):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, SpeechExecutionError):
        return HTTPException(status_code=502, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))
