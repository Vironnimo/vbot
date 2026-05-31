"""FastAPI application factory for the vBot server layer."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

from core.attachments.attachments import (
    AttachmentNotFoundError,
    AttachmentStore,
    AttachmentTooLargeError,
    AttachmentTypeNotAllowedError,
)
from core.chat import ChatLoop, CommandDispatcher
from core.compaction import CompactionService, SummarizationStrategy
from core.image import (
    ImageConfigurationError,
    ImageError,
    ImageExecutionError,
    ImageUnsupportedTargetError,
)
from core.runs import ChatRunManager, RunNotFoundError
from core.settings import SettingsValidationError, load_validated_settings_json
from core.speech import (
    SpeechConfigurationError,
    SpeechError,
    SpeechExecutionError,
    SpeechUnsupportedTargetError,
)
from core.utils.config import Config
from core.utils.log_viewer import LogViewer
from server.delegates import bridge_run_to_event_bus, dispatch_rpc
from server.events import ServerEventBus

JsonObject = dict[str, Any]


class ServerBindState(TypedDict):
    """Resolved bind metadata persisted in FastAPI app state."""

    listen_host: str
    listen_port: int
    port_source: str


_FASTAPI_IMPORT_ERROR: ModuleNotFoundError | None

try:
    from fastapi import (  # type: ignore[import-not-found]
        FastAPI,
        HTTPException,
        Request,
        UploadFile,
        WebSocket,
    )
    from fastapi.responses import (  # type: ignore[import-not-found]
        FileResponse,
        Response,
        StreamingResponse,
    )
    from fastapi.staticfiles import StaticFiles  # type: ignore[import-not-found]
    from starlette.websockets import WebSocketDisconnect  # type: ignore[import-not-found]
except ModuleNotFoundError as exc:  # pragma: no cover - exercised when server extra is absent.
    _FASTAPI_IMPORT_ERROR = exc
    FastAPI = None  # type: ignore[assignment,misc]
    FileResponse = Any  # type: ignore[misc,assignment]
    HTTPException = Any  # type: ignore[misc,assignment]
    Request = Any  # type: ignore[misc,assignment]
    Response = Any  # type: ignore[misc,assignment]
    StaticFiles = Any  # type: ignore[misc,assignment]
    StreamingResponse = Any  # type: ignore[misc,assignment]
    UploadFile = Any  # type: ignore[misc,assignment]
    WebSocket = Any  # type: ignore[misc,assignment]
    WebSocketDisconnect = Exception  # type: ignore[misc,assignment]
else:
    _FASTAPI_IMPORT_ERROR = None

if TYPE_CHECKING:
    from fastapi import FastAPI as FastAPIType  # type: ignore[import-not-found]

    from core.runtime import Runtime
else:
    FastAPIType = Any

WEBUI_DIST_DIR = Path(__file__).resolve().parents[1] / "webui" / "dist"
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 8420
DEFAULT_SERVER_PORT_SOURCE = "default"


def create_app(
    *,
    runtime: Any | None = None,
    config: Config | None = None,
    server_bind: ServerBindState | None = None,
) -> FastAPIType:
    """Create the FastAPI app and wire runtime services into app state."""
    if FastAPI is None:
        raise RuntimeError(
            "FastAPI is required to create the server app"
        ) from _FASTAPI_IMPORT_ERROR
    app_runtime = runtime if runtime is not None else _build_default_runtime(config)
    resolved_server_bind = _resolve_server_bind(
        config=config or _runtime_config(app_runtime),
        server_bind=server_bind,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPIType) -> AsyncIterator[None]:
        app_runtime.start()
        _initialize_app_state(app, app_runtime, server_bind=resolved_server_bind)
        server_logger = logging.getLogger("vbot.server.app")
        server_logger.info(
            "Server application ready on %s:%s",
            resolved_server_bind["listen_host"],
            resolved_server_bind["listen_port"],
        )
        try:
            yield
        finally:
            server_logger.info("Server application stopping")
            _unregister_run_event_bridge(app.state)
            await _shutdown_log_viewer(app.state.log_viewer, server_logger)
            await _shutdown_device_flow_engine(
                getattr(app.state, "device_flow_engine", None),
                server_logger,
            )
            await _shutdown_runtime(app_runtime)

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health() -> JsonObject:
        return {"status": "ok"}

    @app.post("/api/rpc")
    async def rpc(request: Request) -> JsonObject:
        payload = await request.json()
        return await dispatch_rpc(request.app.state, payload)

    @app.post("/api/upload")
    async def upload_attachment(request: Request, file: UploadFile) -> JsonObject:
        attachment_store = _runtime_attachment_store(request.app.state.runtime)
        filename = file.filename or "upload.bin"
        try:
            record = attachment_store.store(filename, await file.read())
        except AttachmentTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except AttachmentTypeNotAllowedError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        finally:
            await file.close()

        return {
            "attachment_id": record.id,
            "filename": record.filename,
            "media_type": record.media_type,
            "size_bytes": record.size_bytes,
            "text_content": record.text_content,
        }

    @app.get("/api/attachments/{attachment_id}")
    async def get_attachment(request: Request, attachment_id: str) -> FileResponse:
        attachment_store = _runtime_attachment_store(request.app.state.runtime)
        try:
            record = attachment_store.get(attachment_id)
        except AttachmentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(record.file_path, media_type=record.media_type)

    @app.post("/api/speech/transcribe")
    async def transcribe_speech(request: Request, file: UploadFile) -> JsonObject:
        speech_service = _runtime_speech_service(request.app.state.runtime)
        filename = file.filename or "recording.webm"
        media_type = file.content_type or "application/octet-stream"
        try:
            result = await speech_service.transcribe(
                await file.read(),
                filename=filename,
                media_type=media_type,
            )
        except SpeechError as exc:
            raise _speech_http_exception(exc) from exc
        finally:
            await file.close()
        return cast(JsonObject, result.to_dict())

    @app.post("/api/speech/synthesize")
    async def synthesize_speech(request: Request) -> Response:
        speech_service = _runtime_speech_service(request.app.state.runtime)
        payload = await request.json()
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="text must be a non-empty string")
        try:
            result = await speech_service.synthesize(text)
        except SpeechError as exc:
            raise _speech_http_exception(exc) from exc
        return Response(content=result.audio, media_type=result.media_type)

    @app.get("/api/speech/artifacts/{artifact_id}")
    async def get_speech_artifact(request: Request, artifact_id: str) -> FileResponse:
        speech_service = _runtime_speech_service(request.app.state.runtime)
        try:
            artifact = speech_service.get_artifact(artifact_id)
        except SpeechError as exc:
            raise _speech_http_exception(exc) from exc
        return FileResponse(
            artifact.file_path,
            media_type=artifact.media_type,
            filename=artifact.filename,
        )

    @app.get("/api/images/artifacts/{artifact_id}")
    async def get_image_artifact(request: Request, artifact_id: str) -> FileResponse:
        image_service = _runtime_image_service(request.app.state.runtime)
        try:
            artifact = image_service.get_artifact(artifact_id)
        except ImageError as exc:
            raise _image_http_exception(exc) from exc
        return FileResponse(
            artifact.file_path,
            media_type=artifact.media_type,
            filename=artifact.filename,
        )

    @app.get("/api/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str) -> StreamingResponse:
        chat_runs = _app_chat_runs(request.app.state)
        try:
            run = chat_runs.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        after_sequence = _replay_after_sequence(request)
        return StreamingResponse(
            _sse_run_events(run, after_sequence=after_sequence),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.websocket("/ws")
    async def websocket_events(websocket: WebSocket) -> None:
        await websocket.accept()
        after_sequence = _parse_after_sequence(websocket.query_params.get("after_sequence"))
        try:
            async for event in websocket.app.state.event_bus.subscribe(
                after_sequence=after_sequence
            ):
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/logs")
    async def websocket_logs(websocket: WebSocket) -> None:
        await websocket.accept()
        file_name = websocket.query_params.get("file")
        cursor = websocket.query_params.get("cursor")
        stream = websocket.app.state.log_viewer.subscribe(file_name or "", cursor=cursor)
        try:
            await _stream_log_events(websocket, stream)
        except ValueError as exc:
            await websocket.close(code=1008, reason=str(exc))
        except FileNotFoundError as exc:
            await websocket.close(code=1008, reason=str(exc))
        except WebSocketDisconnect:
            return
        finally:
            await _close_log_stream(stream)

    _mount_webui(app)

    return app


def _initialize_app_state(
    app: FastAPIType, runtime: Runtime, *, server_bind: ServerBindState
) -> None:
    app.state.runtime = runtime
    app.state.chat_runs = _runtime_chat_runs(runtime)
    app.state.event_bus = ServerEventBus()
    app.state.run_event_bridge_run_ids = OrderedDict()
    app.state.run_event_bridge_unsubscribe = _register_run_event_bridge(app.state)
    app.state.compaction_service = CompactionService(SummarizationStrategy())
    chat_loop = _runtime_chat_loop(runtime)
    chat_loop._compaction_service = app.state.compaction_service
    app.state.chat_loop = chat_loop

    streaming_chat_loop = getattr(runtime, "streaming_chat_loop", None)
    if streaming_chat_loop is not None:
        streaming_chat_loop._compaction_service = app.state.compaction_service

    app.state.command_dispatcher = _runtime_command_dispatcher(runtime)
    app.state.log_viewer = LogViewer(_runtime_data_dir(runtime))
    app.state.agent_delete_lock = asyncio.Lock()
    app.state.server_bind = dict(server_bind)


def _register_run_event_bridge(state: Any) -> Any:
    chat_runs = _app_chat_runs(state)
    add_callback = getattr(chat_runs, "add_run_started_callback", None)
    if not callable(add_callback):
        return None
    return add_callback(lambda run: bridge_run_to_event_bus(state, run))


def _unregister_run_event_bridge(state: Any) -> None:
    unsubscribe = getattr(state, "run_event_bridge_unsubscribe", None)
    if callable(unsubscribe):
        unsubscribe()
    state.run_event_bridge_unsubscribe = None


def _runtime_chat_runs(runtime: Any) -> ChatRunManager:
    run_manager = getattr(runtime, "chat_runs", None)
    if isinstance(run_manager, ChatRunManager):
        return run_manager

    try:
        run_manager = runtime.chat_run_manager
    except AttributeError:
        run_manager = None
    except RuntimeError:
        if runtime.__class__.__name__ == "Runtime" and runtime.__class__.__module__.startswith(
            "core.runtime"
        ):
            raise
        run_manager = None
    if isinstance(run_manager, ChatRunManager):
        runtime.chat_runs = run_manager
        return run_manager

    run_manager = ChatRunManager()
    runtime.chat_runs = run_manager
    return run_manager


def _runtime_chat_loop(runtime: Any) -> Any:
    try:
        chat_loop = runtime.chat_loop
    except AttributeError:
        chat_loop = getattr(runtime, "_chat_loop", None)
    except RuntimeError:
        if runtime.__class__.__name__ == "Runtime" and runtime.__class__.__module__.startswith(
            "core.runtime"
        ):
            raise
        chat_loop = getattr(runtime, "_chat_loop", None)
    if chat_loop is not None:
        return chat_loop
    return ChatLoop(runtime)


def _runtime_command_dispatcher(runtime: Any) -> CommandDispatcher:
    try:
        command_dispatcher = runtime.command_dispatcher
    except AttributeError:
        command_dispatcher = getattr(runtime, "_command_dispatcher", None)
    except RuntimeError:
        if runtime.__class__.__name__ == "Runtime" and runtime.__class__.__module__.startswith(
            "core.runtime"
        ):
            raise
        command_dispatcher = getattr(runtime, "_command_dispatcher", None)
    if isinstance(command_dispatcher, CommandDispatcher):
        return command_dispatcher
    return CommandDispatcher(ChatRunManager())


def _runtime_attachment_store(runtime: Any) -> AttachmentStore:
    try:
        attachment_store = runtime.attachment_store
    except (AttributeError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="Attachment store is unavailable") from exc

    if not isinstance(attachment_store, AttachmentStore):
        raise HTTPException(status_code=503, detail="Attachment store is unavailable")

    return attachment_store


def _runtime_speech_service(runtime: Any) -> Any:
    try:
        return runtime.speech
    except (AttributeError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="Speech service is unavailable") from exc


def _speech_http_exception(error: SpeechError) -> HTTPException:
    if isinstance(error, SpeechConfigurationError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, SpeechUnsupportedTargetError):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, SpeechExecutionError):
        return HTTPException(status_code=502, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


def _runtime_image_service(runtime: Any) -> Any:
    try:
        return runtime.image
    except (AttributeError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="Image service is unavailable") from exc


def _image_http_exception(error: ImageError) -> HTTPException:
    if isinstance(error, ImageConfigurationError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, ImageUnsupportedTargetError):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, ImageExecutionError):
        return HTTPException(status_code=502, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


def _app_chat_runs(state: Any) -> ChatRunManager:
    run_manager = getattr(state, "chat_runs", None)
    if isinstance(run_manager, ChatRunManager):
        return run_manager

    runtime = getattr(state, "runtime", None)
    runtime_run_manager = getattr(runtime, "chat_runs", None)
    if isinstance(runtime_run_manager, ChatRunManager):
        state.chat_runs = runtime_run_manager
        return runtime_run_manager

    raise HTTPException(status_code=503, detail="Chat run manager is unavailable")


def _build_default_runtime(config: Config | None) -> Any:
    from core.runtime import Runtime

    return Runtime(config or Config())


def _resolve_server_bind(
    *, config: Config | None, server_bind: ServerBindState | None
) -> ServerBindState:
    if server_bind is not None:
        return {
            "listen_host": _coerce_bind_host(server_bind.get("listen_host")),
            "listen_port": _coerce_bind_port(
                server_bind.get("listen_port"),
                source="server_bind.listen_port",
            ),
            "port_source": _coerce_bind_port_source(server_bind.get("port_source")),
        }

    if config is None:
        return _default_server_bind()

    if environment_port := os.environ.get("VBOT_SERVER_PORT"):
        return {
            "listen_host": DEFAULT_SERVER_HOST,
            "listen_port": _coerce_bind_port(environment_port, source="VBOT_SERVER_PORT"),
            "port_source": "VBOT_SERVER_PORT",
        }

    settings_path = config.data_dir / "settings.json"
    try:
        data = load_validated_settings_json(settings_path)
    except SettingsValidationError as exc:
        raise ValueError(str(exc)) from exc
    if data:
        for key in ("server_port", "SERVER_PORT", "port", "PORT"):
            value = data.get(key)
            if value is not None:
                return {
                    "listen_host": DEFAULT_SERVER_HOST,
                    "listen_port": _coerce_bind_port(value, source=f"settings.{key}"),
                    "port_source": f"settings.{key}",
                }

    return _default_server_bind()


def _default_server_bind() -> ServerBindState:
    return {
        "listen_host": DEFAULT_SERVER_HOST,
        "listen_port": DEFAULT_SERVER_PORT,
        "port_source": DEFAULT_SERVER_PORT_SOURCE,
    }


def _runtime_config(runtime: Any) -> Config | None:
    config = getattr(runtime, "_config", None)
    if isinstance(config, Config):
        return config
    return None


def _runtime_data_dir(runtime: Any) -> Path:
    data_dir = getattr(runtime, "_data_dir", None)
    if isinstance(data_dir, Path):
        return data_dir

    storage = getattr(runtime, "storage", None)
    storage_data_dir = getattr(storage, "data_dir", None)
    if isinstance(storage_data_dir, Path):
        return storage_data_dir

    config = _runtime_config(runtime)
    if config is not None:
        return Path(config.data_dir).expanduser()

    raise RuntimeError("Runtime data directory is unavailable")


def _coerce_bind_host(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return DEFAULT_SERVER_HOST
    return value


def _coerce_bind_port(value: Any, *, source: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be an integer port") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{source} must be between 1 and 65535")
    return port


def _coerce_bind_port_source(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return DEFAULT_SERVER_PORT_SOURCE
    return value


def _mount_webui(app: FastAPIType) -> None:
    webui_dist_dir = WEBUI_DIST_DIR
    webui_index_file = webui_dist_dir / "index.html"
    if not webui_dist_dir.is_dir() or not webui_index_file.is_file():
        return

    webui_assets_dir = webui_dist_dir / "assets"
    if webui_assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=webui_assets_dir), name="webui-assets")

    @app.get("/", include_in_schema=False)
    async def webui_index() -> FileResponse:
        return FileResponse(webui_index_file)

    @app.get("/{path:path}", include_in_schema=False)
    async def webui_fallback(path: str) -> FileResponse:
        if _is_reserved_server_path(path):
            raise HTTPException(status_code=404, detail="Not Found")
        requested_file = _safe_webui_file_path(webui_dist_dir, path)
        if requested_file is not None:
            return FileResponse(requested_file)
        return FileResponse(webui_index_file)


async def _stream_log_events(websocket: WebSocket, stream: Any) -> None:
    stream_iter = stream.__aiter__()
    disconnect_task = asyncio.create_task(websocket.receive())
    try:
        while True:
            event_task = asyncio.create_task(stream_iter.__anext__())
            done, _pending = await asyncio.wait(
                {event_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if disconnect_task in done:
                message = disconnect_task.result()
                if message.get("type") == "websocket.disconnect":
                    event_task.cancel()
                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                        await event_task
                    return
                disconnect_task = asyncio.create_task(websocket.receive())

            if event_task in done:
                try:
                    event = event_task.result()
                except StopAsyncIteration:
                    return
                await websocket.send_json(event)
            else:
                event_task.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await event_task
    finally:
        disconnect_task.cancel()
        with suppress(asyncio.CancelledError):
            await disconnect_task


async def _shutdown_log_viewer(log_viewer: LogViewer, logger: logging.Logger) -> None:
    try:
        await asyncio.wait_for(log_viewer.aclose(), timeout=1)
    except TimeoutError:
        logger.warning("Timed out while shutting down log viewer")


async def _shutdown_runtime(runtime: Any) -> None:
    aclose = getattr(runtime, "aclose", None)
    if callable(aclose):
        await aclose()
        return
    runtime.stop()


async def _shutdown_device_flow_engine(engine: Any, logger: logging.Logger) -> None:
    if engine is None:
        return
    aclose = getattr(engine, "aclose", None)
    if not callable(aclose):
        return
    try:
        await asyncio.wait_for(aclose(), timeout=1)
    except TimeoutError:
        logger.warning("Timed out while shutting down OAuth device flow engine")


async def _close_log_stream(stream: Any) -> None:
    try:
        await asyncio.wait_for(stream.aclose(), timeout=1)
    except TimeoutError:
        return


def _is_reserved_server_path(path: str) -> bool:
    return path == "health" or path == "ws" or path.startswith("api/")


def _parse_after_sequence(raw: str | None) -> int:
    """Parse the after_sequence query param, clamping to int ≥ 0 with 0 on failure."""
    if raw is None:
        return 0
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return 0
    return max(value, 0)


def _replay_after_sequence(request: Request) -> int:
    if "after_sequence" in request.query_params:
        return _parse_after_sequence(request.query_params.get("after_sequence"))
    return _parse_after_sequence(request.headers.get("last-event-id"))


def _safe_webui_file_path(webui_dist_dir: Path, requested_path: str) -> Path | None:
    file_path = webui_dist_dir / requested_path
    try:
        resolved_file_path = file_path.resolve()
        resolved_dist_dir = webui_dist_dir.resolve()
        resolved_file_path.relative_to(resolved_dist_dir)
    except ValueError:
        return None

    if resolved_file_path.is_file():
        return resolved_file_path
    return None


async def _sse_run_events(run: Any, *, after_sequence: int = 0) -> AsyncIterator[str]:
    async for event in run.subscribe(after_sequence=after_sequence):
        data = _remove_opaque_provider_metadata(event.to_dict())
        yield (
            f"id: {event.sequence}\n"
            f"event: {event.type}\n"
            f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
        )


def _remove_opaque_provider_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_opaque_provider_metadata(item)
            for key, item in value.items()
            if key != "reasoning_meta"
        }
    if isinstance(value, list):
        return [_remove_opaque_provider_metadata(item) for item in value]
    return value
