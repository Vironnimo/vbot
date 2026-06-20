"""FastAPI application factory for the vBot server layer."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

from core.attachments.attachments import (
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    AttachmentTypeNotAllowedError,
)
from core.model_tasks import (
    ImageConfigurationError,
    ImageError,
    ImageExecutionError,
    ImageUnsupportedTargetError,
    SpeechConfigurationError,
    SpeechError,
    SpeechExecutionError,
    SpeechUnsupportedTargetError,
)
from core.runs import ChatRunManager, RunNotFoundError, RunStatus
from core.settings import SettingsValidationError, load_validated_settings_json
from core.utils.config import Config
from core.utils.log_viewer import LogViewer
from server.clients import ClientRegistry
from server.delegates import RPC_ERROR_INVALID_REQUEST, bridge_run_to_event_bus, dispatch_rpc
from server.events import RESOURCE_KIND_CLIENTS, ServerEventBus
from server.rpc.event_bridge import publish_resource_changed

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
UPLOAD_READ_CHUNK_SIZE_BYTES = 1_048_576


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
        await _fire_extension_startup(app_runtime)
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
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": {
                    "code": RPC_ERROR_INVALID_REQUEST,
                    "message": "RPC request body must be valid JSON",
                },
            }
        return await dispatch_rpc(request.app.state, payload)

    @app.post("/api/upload")
    async def upload_attachment(request: Request, file: UploadFile) -> JsonObject:
        attachment_store = request.app.state.runtime.attachment_store
        filename = file.filename or "upload.bin"
        try:
            data = await _read_upload_file_with_limit(
                file,
                max_size_bytes=attachment_store.max_size_bytes,
                upload_kind="Attachment",
            )
            record = attachment_store.store(filename, data)
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
        attachment_store = request.app.state.runtime.attachment_store
        try:
            record = attachment_store.get(attachment_id)
        except AttachmentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(record.file_path, media_type=record.media_type)

    @app.post("/api/speech/transcribe")
    async def transcribe_speech(request: Request, file: UploadFile) -> JsonObject:
        runtime = request.app.state.runtime
        speech_service = runtime.speech
        filename = file.filename or "recording.webm"
        media_type = file.content_type or "application/octet-stream"
        try:
            audio = await _read_upload_file_with_limit(
                file,
                max_size_bytes=runtime.speech_upload_max_size_bytes,
                upload_kind="Speech audio",
            )
            result = await speech_service.transcribe(
                audio,
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
        speech_service = request.app.state.runtime.speech
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="Request body must be valid JSON",
            ) from exc
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

    @app.get("/api/images/artifacts/{artifact_id}")
    async def get_image_artifact(request: Request, artifact_id: str) -> FileResponse:
        image_service = request.app.state.runtime.image
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
        event_bus = websocket.app.state.event_bus
        client_epoch = _parse_query_string(websocket.query_params.get("epoch"))
        client_after_sequence = _parse_after_sequence(websocket.query_params.get("after_sequence"))
        # Register this window in the presence roster *before* reading the hello
        # high-water mark and *before* the subscribe loop. Publishing the connect
        # signal first means its sequence sits at or below the live-only floor,
        # so this window does not replay its own presence event while other
        # windows still see it; the floor is read before the hello send, so
        # events arriving during that send are not skipped (no replay gap).
        client_entry = _register_ws_client(websocket)
        # Read last_sequence *before* sending the hello frame so any events
        # published during the await are still in the retained deque and get
        # replayed by the subsequent subscribe.
        last_sequence_at_hello = _bus_last_sequence(event_bus)
        active_runs = _active_runs_snapshot(websocket.app.state)
        hello_frame: JsonObject = {
            "type": "connection_ready",
            "epoch": _bus_epoch(event_bus),
            "last_sequence": last_sequence_at_hello,
            "active_runs": active_runs,
        }
        await websocket.send_json(hello_frame)
        if (
            hello_frame["epoch"]
            and client_epoch == hello_frame["epoch"]
            and client_after_sequence > 0
        ):
            subscribe_after_sequence = client_after_sequence
        else:
            subscribe_after_sequence = last_sequence_at_hello
        try:
            async for event in event_bus.subscribe(after_sequence=subscribe_after_sequence):
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return
        finally:
            _unregister_ws_client(websocket.app.state, client_entry)

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
    app.state.chat_runs = runtime.chat_run_manager
    app.state.event_bus = ServerEventBus()
    app.state.client_registry = ClientRegistry()
    app.state.run_event_bridge_run_ids = OrderedDict()
    app.state.run_event_bridge_unsubscribe = _register_run_event_bridge(app.state)
    app.state.chat_loop = runtime.chat_loop
    app.state.streaming_chat_loop = runtime.streaming_chat_loop
    app.state.command_dispatcher = runtime.command_dispatcher
    app.state.log_viewer = LogViewer(runtime.storage.data_dir)
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


async def _read_upload_file_with_limit(
    file: UploadFile,
    *,
    max_size_bytes: int,
    upload_kind: str,
) -> bytes:
    chunks: list[bytes] = []
    size_bytes = 0
    while True:
        read_size = min(UPLOAD_READ_CHUNK_SIZE_BYTES, max_size_bytes - size_bytes + 1)
        chunk = await file.read(read_size)
        if not chunk:
            return b"".join(chunks)
        size_bytes += len(chunk)
        if size_bytes > max_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{upload_kind} size {size_bytes} exceeds limit {max_size_bytes}",
            )
        chunks.append(chunk)


def _speech_http_exception(error: SpeechError) -> HTTPException:
    if isinstance(error, SpeechConfigurationError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, SpeechUnsupportedTargetError):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, SpeechExecutionError):
        return HTTPException(status_code=502, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


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
    """Read the runtime's public config when present — bind resolution runs pre-start."""
    config = getattr(runtime, "config", None)
    if isinstance(config, Config):
        return config
    return None


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


async def _fire_extension_startup(runtime: Any) -> None:
    fire = getattr(runtime, "fire_extension_startup", None)
    if callable(fire):
        await fire()


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


def _parse_query_string(raw: str | None) -> str:
    """Return the query string value as-is, or empty when absent/blank."""
    if raw is None:
        return ""
    return raw.strip()


def _register_ws_client(websocket: WebSocket) -> Any:
    """Register the connecting window in the presence roster, if one is wired.

    Reads the client-minted connection id and accessor type from the query
    params and the browser/OS from the ``User-Agent`` header, then publishes a
    ``clients`` reload-on-change signal so other windows refresh the roster.
    Returns the registry entry (the unregister handle) or ``None`` when no
    registry exists (CLI-only runtime stub).
    """
    registry = getattr(websocket.app.state, "client_registry", None)
    if registry is None:
        return None
    entry = registry.register(
        connection_id=_parse_query_string(websocket.query_params.get("connection_id")),
        accessor=_parse_query_string(websocket.query_params.get("accessor")),
        user_agent=websocket.headers.get("user-agent", ""),
    )
    publish_resource_changed(websocket.app.state, RESOURCE_KIND_CLIENTS)
    return entry


def _unregister_ws_client(state: Any, entry: Any) -> None:
    """Remove a previously registered window and signal the roster change."""
    if entry is None:
        return
    registry = getattr(state, "client_registry", None)
    if registry is None:
        return
    registry.unregister(entry.id)
    publish_resource_changed(state, RESOURCE_KIND_CLIENTS)


def _bus_epoch(event_bus: ServerEventBus) -> str:
    """Return the event bus generation epoch."""
    return event_bus.epoch


def _bus_last_sequence(event_bus: ServerEventBus) -> int:
    """Return the bus's last issued sequence number."""
    return event_bus.last_sequence


def _active_runs_snapshot(state: Any) -> list[JsonObject]:
    """Build the active-runs list for the connection_ready hello frame.

    Returns an empty list when the chat run manager is unavailable so the
    handshake can still complete — the snapshot is connection-specific and
    the client treats empty ``active_runs`` as authoritative for that scope.
    """
    try:
        chat_runs = _app_chat_runs(state)
    except HTTPException:
        return []
    snapshot: list[JsonObject] = []
    active_runs = getattr(chat_runs, "active_runs", None)
    if not callable(active_runs):
        return snapshot
    for run in active_runs():
        if run.status != RunStatus.RUNNING:
            continue
        snapshot.append(
            {
                "run_id": run.id,
                "agent_id": run.agent_id,
                # Bare ``agent_id`` plus project so a reconnecting client can
                # rebuild the address-keyed session and re-attach the run.
                "project_id": run.project_id,
                "session_id": run.session_id,
                "status": RunStatus.RUNNING.value,
                "sse_url": f"/api/runs/{run.id}/events",
            }
        )
    return snapshot


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


async def _sse_run_events(run: Any, *, after_sequence: int = 0) -> AsyncGenerator[str, None]:
    async with aclosing(run.subscribe(after_sequence=after_sequence)) as events:
        async for event in events:
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
