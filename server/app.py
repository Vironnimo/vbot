"""FastAPI HTTP routes and application factory."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast

from core.attachments.attachments import (
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    AttachmentTypeNotAllowedError,
)
from core.model_tasks import (
    SpeechConfigurationError,
    SpeechError,
    SpeechExecutionError,
    SpeechUnsupportedTargetError,
)
from core.model_tasks.speech_types import SpeechProgress
from core.runs import RunNotFoundError
from core.tools.terminal_manager import TerminalNotFoundError
from core.utils.config import Config
from core.utils.server_control import (
    CONTROL_SHUTDOWN_PATH,
    CONTROL_TOKEN_HEADER,
    is_authorized_control_token,
)
from server._app_lifecycle import (
    _app_chat_runs,
    _fire_extension_startup,
    _initialize_app_state,
    _shutdown_device_flow_engine,
    _shutdown_local_catalog_refresh,
    _shutdown_log_viewer,
    _shutdown_model_list_refreshes,
    _shutdown_runtime,
    _shutdown_statistics_warmup,
    _start_statistics_warmup,
    _unregister_bash_process_change_bridge,
    _unregister_calendar_change_bridge,
    _unregister_cron_change_bridge,
    _unregister_run_event_bridge,
    _unregister_session_completion_read_bridge,
    _unregister_session_title_bridge,
    _unregister_terminal_change_bridge,
)
from server._bind import ServerBindState, _resolve_server_bind, _runtime_config
from server._http_dependencies import (
    _FASTAPI_IMPORT_ERROR,
    FastAPI,
    FastAPIType,
    FileResponse,
    HTTPException,
    MultiPartException,
    MultiPartParser,
    RedirectResponse,
    Request,
    Response,
    StarletteUploadFile,
    StaticFiles,
    StreamingResponse,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from server._origins import _BrowserOriginGuardMiddleware, _configured_browser_origins
from server._streams import (
    REPLAY_STATUS_RESUMED,
    _active_runs_snapshot,
    _bus_epoch,
    _bus_last_sequence,
    _close_log_stream,
    _connection_replay_status,
    _parse_after_sequence,
    _parse_query_string,
    _queues_snapshot,
    _register_ws_client,
    _replay_after_sequence,
    _sse_run_events,
    _stream_websocket_events,
    _unregister_ws_client,
)
from server.file_delivery import PREVIEW_URL_PREFIX
from server.rpc.errors import RPC_ERROR_INTERNAL, RPC_ERROR_INVALID_REQUEST
from server.rpc.methods import dispatch_rpc
from server.rpc.operations_methods import FILE_PREVIEW_WORKERS

JsonObject = dict[str, Any]

WEBUI_DIST_DIR = Path(__file__).resolve().parents[1] / "webui" / "dist"

WEBUI_DOCUMENT_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

UPLOAD_READ_CHUNK_SIZE_BYTES = 1_048_576

MULTIPART_BODY_OVERHEAD_ALLOWANCE_BYTES = 65_536

MULTIPART_MAX_FORM_FIELDS = 16

JSON_MEDIA_TYPE = "application/json"

JSON_REQUEST_BODY_MAX_BYTES = 1_048_576


class _UploadTooLargeMultipartError(MultiPartException):  # type: ignore[misc]
    """Abort multipart parsing before an oversized file part is spooled."""


class _SizeLimitedMultiPartParser(MultiPartParser):  # type: ignore[misc]
    """Starlette multipart parser with an exact per-file byte limit."""

    def __init__(
        self,
        *args: Any,
        max_file_size_bytes: int,
        upload_kind: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._max_file_size_bytes = max_file_size_bytes
        self._upload_kind = upload_kind
        self._current_file_size_bytes = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_size_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        part_size_bytes = end - start
        if self._current_part.file is not None:
            next_size_bytes = self._current_file_size_bytes + part_size_bytes
            if next_size_bytes > self._max_file_size_bytes:
                raise _UploadTooLargeMultipartError(
                    f"{self._upload_kind} size exceeds limit {self._max_file_size_bytes}"
                )
            self._current_file_size_bytes = next_size_bytes
        super().on_part_data(data, start, end)


def _require_json_media_type(request: Request) -> None:
    content_type = request.headers.get("content-type")
    media_type = content_type.partition(";")[0].strip().casefold() if content_type else ""
    if media_type != JSON_MEDIA_TYPE:
        raise HTTPException(
            status_code=415,
            detail="Content-Type must be application/json",
        )


async def _read_json_body_with_limit(request: Request) -> bytes:
    """Read a JSON request body incrementally without allowing unbounded buffering."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Content-Length must be an integer"
            ) from exc
        if declared_size < 0:
            raise HTTPException(status_code=400, detail="Content-Length must not be negative")
        if declared_size > JSON_REQUEST_BODY_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Request body exceeds size limit")

    chunks: list[bytes] = []
    size_bytes = 0
    async for chunk in request.stream():
        size_bytes += len(chunk)
        if size_bytes > JSON_REQUEST_BODY_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Request body exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json_payload_with_limit(request: Request) -> object:
    body = await _read_json_body_with_limit(request)
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Request body must be valid JSON") from exc


def create_app(
    *,
    runtime: Any | None = None,
    config: Config | None = None,
    server_bind: ServerBindState | None = None,
    shutdown_token: str | None = None,
    request_shutdown: Callable[[], None] | None = None,
    request_restart: Callable[[], None] | None = None,
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
        app.state.request_restart = request_restart
        app.state.statistics_warmup_task = _start_statistics_warmup(app.state)
        await _fire_extension_startup(app_runtime)
        # Local model catalogs (auto_refresh connections, e.g. Ollama) refresh
        # in the background — never blocking startup; the method itself is
        # throttled and swallows failures. Guarded for stub runtimes in tests.
        maybe_refresh_local_catalogs = getattr(app_runtime, "maybe_refresh_local_catalogs", None)
        if callable(maybe_refresh_local_catalogs):
            app.state.local_catalog_refresh_task = asyncio.create_task(
                maybe_refresh_local_catalogs()
            )
        server_logger = logging.getLogger("vbot.server.app")
        server_logger.info(
            "Server application ready on %s:%s",
            resolved_server_bind["listen_host"],
            resolved_server_bind["listen_port"],
        )
        activate_bootstrap = getattr(app_runtime, "activate_bootstrap", None)
        if callable(activate_bootstrap):
            activate_bootstrap()
        try:
            yield
        finally:
            server_logger.info("Server application stopping")
            await _shutdown_local_catalog_refresh(
                getattr(app.state, "local_catalog_refresh_task", None),
                server_logger,
            )
            await _shutdown_statistics_warmup(getattr(app.state, "statistics_warmup_task", None))
            _unregister_run_event_bridge(app.state)
            _unregister_session_title_bridge(app.state)
            _unregister_session_completion_read_bridge(app.state)
            _unregister_cron_change_bridge(app.state)
            _unregister_calendar_change_bridge(app.state)
            _unregister_terminal_change_bridge(app.state)
            _unregister_bash_process_change_bridge(app.state)
            await _shutdown_log_viewer(app.state.log_viewer, server_logger)
            await _shutdown_device_flow_engine(
                getattr(app.state, "device_flow_engine", None),
                server_logger,
            )
            await _shutdown_model_list_refreshes(app_runtime)
            await _shutdown_runtime(app_runtime)

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(
        _BrowserOriginGuardMiddleware,
        allowed_origins=_configured_browser_origins(resolved_server_bind),
        same_origin_ip_port=(
            resolved_server_bind["listen_port"]
            if resolved_server_bind["listen_host"] in {"0.0.0.0", "::"}
            else None
        ),
    )

    @app.get("/health")
    async def health() -> JsonObject:
        return {"status": "ok"}

    @app.post(CONTROL_SHUTDOWN_PATH, status_code=202, include_in_schema=False)
    async def shutdown(request: Request) -> JsonObject:
        provided_token = request.headers.get(CONTROL_TOKEN_HEADER)
        if not is_authorized_control_token(provided_token, shutdown_token):
            raise HTTPException(status_code=404)
        if request_shutdown is None:
            raise HTTPException(status_code=503, detail="Server shutdown is unavailable")
        request_shutdown()
        return {"status": "stopping"}

    @app.post("/api/rpc")
    async def rpc(request: Request, response: Response) -> JsonObject:
        _require_json_media_type(request)
        try:
            payload = await _read_json_payload_with_limit(request)
        except ValueError:
            return {
                "ok": False,
                "error": {
                    "code": RPC_ERROR_INVALID_REQUEST,
                    "message": "RPC request body must be valid JSON",
                },
            }
        try:
            return await dispatch_rpc(request.app.state, payload)
        except Exception:
            # The dispatcher logged the original failure; never expose its details.
            response.status_code = 500
            return {
                "ok": False,
                "error": {"code": RPC_ERROR_INTERNAL, "message": "Internal server error"},
            }

    @app.post("/api/upload")
    async def upload_attachment(request: Request) -> JsonObject:
        attachment_store = request.app.state.runtime.attachment_store
        file = await _parse_upload_file_with_limit(
            request,
            max_size_bytes=attachment_store.max_size_bytes,
            upload_kind="Attachment",
        )
        filename = file.filename or "upload"
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
        }

    @app.get("/api/attachments/{attachment_id}")
    async def get_attachment(request: Request, attachment_id: str) -> FileResponse:
        attachment_store = request.app.state.runtime.attachment_store
        try:
            record = attachment_store.get(attachment_id)
        except AttachmentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            record.file_path,
            media_type=record.media_type,
            filename=record.filename,
            content_disposition_type="inline",
        )

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
            return StreamingResponse(
                _stream_speech(
                    lambda progress: speech_service.transcribe(
                        audio, filename=filename, media_type=media_type, progress=progress
                    )
                ),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        try:
            result = await speech_service.transcribe(
                audio, filename=filename, media_type=media_type
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
            return StreamingResponse(
                _stream_speech(
                    lambda progress: speech_service.synthesize_artifact(text, progress=progress)
                ),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
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

    @app.get("/api/files/{token}")
    async def get_assistant_file(request: Request, token: str, download: bool = False) -> Response:
        delivery = request.app.state.file_delivery
        delivered = await FILE_PREVIEW_WORKERS.run(delivery.resolve_token, token)
        if delivered is None:
            raise HTTPException(status_code=404)
        if delivered.media_type == "text/html" and not download:
            try:
                preview = await FILE_PREVIEW_WORKERS.run(
                    delivery.open_preview, f"/api/files/{token}"
                )
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=404) from exc
            return RedirectResponse(
                preview["url"],
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        return FileResponse(
            delivered.path,
            media_type=delivered.media_type,
            filename=delivered.path.name,
            content_disposition_type=(
                "inline" if delivered.inline and not download else "attachment"
            ),
            headers=delivered.response_headers,
        )

    @app.get("/api/preview-assets/{token}/.revision")
    async def get_preview_revision(request: Request, token: str) -> Response:
        delivery = request.app.state.file_delivery
        try:
            revision = await FILE_PREVIEW_WORKERS.run(delivery.preview_revision, token)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=404) from exc
        return Response(
            json.dumps({"revision": revision}),
            media_type="application/json",
            headers=delivery.preview_headers(str(request.base_url), token),
        )

    @app.api_route("/api/preview-assets/{token}/{asset_path:path}", methods=["GET", "HEAD"])
    async def get_preview_asset(request: Request, token: str, asset_path: str) -> Response:
        delivery = request.app.state.file_delivery
        try:
            delivered = await FILE_PREVIEW_WORKERS.run(delivery.preview_file, token, asset_path)
            headers = delivery.preview_headers(str(request.base_url), token)
            if (
                delivered.path.name == "index.html"
                and asset_path
                and not asset_path.endswith("/")
                and asset_path.rsplit("/", 1)[-1] != "index.html"
            ):
                return RedirectResponse(
                    str(request.url.replace(path=request.url.path + "/")), headers=headers
                )
            if delivered.media_type == "text/html":
                headers["Content-Disposition"] = "inline"
                content = await FILE_PREVIEW_WORKERS.run(
                    delivery.preview_html, delivered.path, f"{PREVIEW_URL_PREFIX}{token}/.revision"
                )
                return Response(content, media_type="text/html", headers=headers)
            return FileResponse(delivered.path, media_type=delivered.media_type, headers=headers)
        except (ValueError, OSError):
            content, headers = await FILE_PREVIEW_WORKERS.run(
                delivery.preview_unavailable, str(request.base_url), token
            )
            return Response(
                content,
                status_code=404,
                media_type="text/html",
                headers=headers,
            )

    @app.api_route("/api/extension-assets/{token}/{asset_path:path}", methods=["GET", "HEAD"])
    async def get_extension_asset(request: Request, token: str, asset_path: str) -> Response:
        """Serve a page asset only while its exact Extension registration survives."""
        delivery = request.app.state.file_delivery
        try:
            claims = await FILE_PREVIEW_WORKERS.run(delivery.extension_page_claims, token)
            from core.extensions import ExtensionRegistrationIdentity

            identity = ExtensionRegistrationIdentity(claims["extension"], claims["epoch"])
            registry = request.app.state.runtime.extensions
            if registry is None or not registry.is_registration_current(identity):
                raise ValueError("Extension page is unavailable")
            declarations = await FILE_PREVIEW_WORKERS.run(registry.page_declarations)
            current = next(
                (
                    (declaration, entry)
                    for candidate, declaration, entry in declarations
                    if candidate == identity and declaration.page_id == claims["page"]
                ),
                None,
            )
            if current is None or current[1].parent != Path(claims["root"]):
                raise ValueError("Extension page is unavailable")
            _claims, delivered = await FILE_PREVIEW_WORKERS.run(
                delivery.extension_page_asset, token, asset_path
            )
            if (
                request.app.state.runtime.extensions is not registry
                or not registry.is_registration_current(identity)
            ):
                raise ValueError("Extension page is unavailable")
            return FileResponse(
                delivered.path,
                media_type=delivered.media_type,
                headers=delivery.extension_page_headers(str(request.base_url), token),
            )
        except (OSError, ValueError, KeyError):
            return Response(status_code=404)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str) -> StreamingResponse:
        chat_runs = _app_chat_runs(request.app.state)
        try:
            run = chat_runs.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        after_sequence = _replay_after_sequence(request)
        return StreamingResponse(
            _sse_run_events(
                run,
                after_sequence=after_sequence,
                file_delivery=request.app.state.file_delivery,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # Prevent reverse proxies (e.g. nginx) from buffering the
                # incremental Run timeline into one late flush.
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/extension-runs/{token}/events")
    async def extension_run_events(request: Request, token: str) -> StreamingResponse:
        """Stream only a Run still owned by the page identity in its capability."""
        delivery = request.app.state.file_delivery
        try:
            claims = delivery.extension_run_claims(token)
            registry = request.app.state.runtime.extensions
            if registry is None:
                raise ValueError("Extension Run is unavailable")
            identity, page = await FILE_PREVIEW_WORKERS.run(
                _current_extension_page,
                registry,
                claims,
            )
            if identity is None or page is None:
                raise ValueError("Extension Run is unavailable")
            host = registry.host_for(identity)
            temporary_agents = host.temporary_agents
            if temporary_agents is None:
                raise ValueError("Extension Run is unavailable")
            inspection = await temporary_agents.owned_run(claims["group_id"], claims["run_id"])
            # The checked host may be retired while awaiting durable ownership.
            # Re-resolve both page and owner before exposing the SSE iterator.
            if request.app.state.runtime.extensions is not registry:
                raise ValueError("Extension Run is unavailable")
            identity, page = await FILE_PREVIEW_WORKERS.run(
                _current_extension_page,
                registry,
                claims,
            )
            if identity is None or page is None:
                raise ValueError("Extension Run is unavailable")
            verified_host = registry.host_for(identity)
            verified_groups = verified_host.temporary_agents
            if verified_groups is None:
                raise ValueError("Extension Run is unavailable")
            verified = await verified_groups.owned_run(claims["group_id"], claims["run_id"])
            if inspection.run is None or verified.run is None:
                raise ValueError("Extension Run is not live")
        except (KeyError, ValueError):
            raise HTTPException(status_code=404, detail="Extension Run is unavailable") from None
        return StreamingResponse(
            _sse_run_events(
                verified.run,
                after_sequence=claims["after_sequence"],
                file_delivery=delivery,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
        try:
            # Read last_sequence *before* sending the hello frame so any events
            # published during the await are still in the retained deque and get
            # replayed by the subsequent subscribe.
            last_sequence_at_hello = _bus_last_sequence(event_bus)
            replay_status = _connection_replay_status(
                event_bus,
                client_epoch=client_epoch,
                client_after_sequence=client_after_sequence,
                last_sequence=last_sequence_at_hello,
            )
            active_runs = _active_runs_snapshot(websocket.app.state)
            hello_frame: JsonObject = {
                "type": "connection_ready",
                "epoch": _bus_epoch(event_bus),
                "last_sequence": last_sequence_at_hello,
                "replay_status": replay_status,
                "active_runs": active_runs,
                "queues": _queues_snapshot(websocket.app.state),
            }
            await websocket.send_json(hello_frame)
            if replay_status == REPLAY_STATUS_RESUMED:
                subscribe_after_sequence = client_after_sequence
            else:
                subscribe_after_sequence = last_sequence_at_hello
            async with aclosing(
                event_bus.subscribe(after_sequence=subscribe_after_sequence)
            ) as stream:
                await _stream_websocket_events(websocket, stream)
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
            await _stream_websocket_events(websocket, stream)
        except ValueError as exc:
            await websocket.close(code=1008, reason=str(exc))
        except FileNotFoundError as exc:
            await websocket.close(code=1008, reason=str(exc))
        except WebSocketDisconnect:
            return
        finally:
            await _close_log_stream(stream)

    @app.websocket("/ws/terminals/{terminal_id}")
    async def websocket_terminal(websocket: WebSocket, terminal_id: str) -> None:
        await websocket.accept()
        manager = getattr(websocket.app.state.runtime, "terminal_manager", None)
        if manager is None:
            await websocket.close(code=1011, reason="Interactive terminals are unavailable")
            return
        try:
            stream = manager.watch_for_operator(terminal_id)
            async with aclosing(stream) as events:
                await _stream_websocket_events(websocket, events)
        except TerminalNotFoundError as exc:
            await websocket.close(code=1008, reason=str(exc))
        except WebSocketDisconnect:
            return

    _mount_webui(app)

    return app


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


async def _parse_upload_file_with_limit(
    request: Request,
    *,
    max_size_bytes: int,
    upload_kind: str,
) -> UploadFile:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.partition(";")[0].strip().casefold()
    if media_type != "multipart/form-data":
        raise HTTPException(status_code=422, detail="multipart file field 'file' is required")

    max_body_size_bytes = max_size_bytes + MULTIPART_BODY_OVERHEAD_ALLOWANCE_BYTES
    content_length = _request_content_length(request)
    if content_length is not None and content_length > max_body_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"{upload_kind} request body exceeds limit {max_body_size_bytes}",
        )

    parser = _SizeLimitedMultiPartParser(
        request.headers,
        _stream_request_body_with_limit(
            request,
            max_body_size_bytes=max_body_size_bytes,
            upload_kind=upload_kind,
        ),
        max_files=1,
        max_fields=MULTIPART_MAX_FORM_FIELDS,
        max_part_size=MULTIPART_BODY_OVERHEAD_ALLOWANCE_BYTES,
        max_file_size_bytes=max_size_bytes,
        upload_kind=upload_kind,
    )
    try:
        form = await parser.parse()
    except _UploadTooLargeMultipartError as exc:
        raise HTTPException(status_code=413, detail=exc.message) from exc
    except MultiPartException as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    file = form.get("file")
    if not isinstance(file, StarletteUploadFile):
        await form.close()
        raise HTTPException(status_code=422, detail="multipart file field 'file' is required")
    return cast(UploadFile, file)


def _request_content_length(request: Request) -> int | None:
    value = request.headers.get("content-length")
    if value is None:
        return None
    try:
        content_length = int(value)
    except ValueError:
        return None
    return content_length if content_length >= 0 else None


async def _stream_request_body_with_limit(
    request: Request,
    *,
    max_body_size_bytes: int,
    upload_kind: str,
) -> AsyncGenerator[bytes, None]:
    received_size_bytes = 0
    async for chunk in request.stream():
        received_size_bytes += len(chunk)
        if received_size_bytes > max_body_size_bytes:
            raise _UploadTooLargeMultipartError(
                f"{upload_kind} request body exceeds limit {max_body_size_bytes}"
            )
        yield chunk


async def _stream_speech(
    operation: Callable[[SpeechProgress], Awaitable[Any]],
) -> AsyncGenerator[str, None]:
    """Request-local progress heartbeats followed by one terminal result."""
    progress = SpeechProgress()
    task = asyncio.ensure_future(operation(progress))
    try:
        while not task.done():
            yield json.dumps({"type": "progress", **progress.snapshot()}) + "\n"
            await asyncio.wait({task}, timeout=0.5)
        try:
            result = task.result()
        except SpeechError as exc:
            error = _speech_http_exception(exc)
            yield (
                json.dumps({"type": "error", "detail": error.detail, "status": error.status_code})
                + "\n"
            )
        except Exception:
            logging.getLogger(__name__).exception("Speech stream failed")
            yield (
                json.dumps({"type": "error", "detail": "Speech request failed", "status": 500})
                + "\n"
            )
        else:
            yield json.dumps({"type": "result", "result": result.to_dict()}) + "\n"
    finally:
        if not task.done():
            task.cancel()
        # Local inference keeps its existing cancellation-safe worker semantics:
        # a disconnected client cannot release an engine still doing work.
        with suppress(asyncio.CancelledError, Exception):
            await task


def _speech_http_exception(error: SpeechError) -> HTTPException:
    if isinstance(error, SpeechConfigurationError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, SpeechUnsupportedTargetError):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, SpeechExecutionError):
        return HTTPException(status_code=502, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


def _build_default_runtime(config: Config | None) -> Any:
    from core.runtime import Runtime

    return Runtime(config or Config())


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
        return FileResponse(webui_index_file, headers=WEBUI_DOCUMENT_CACHE_HEADERS)

    @app.get("/{path:path}", include_in_schema=False)
    async def webui_fallback(path: str) -> FileResponse:
        if _is_reserved_server_path(path):
            raise HTTPException(status_code=404, detail="Not Found")
        requested_file = _safe_webui_file_path(webui_dist_dir, path)
        if requested_file is not None:
            return FileResponse(requested_file)
        return FileResponse(webui_index_file, headers=WEBUI_DOCUMENT_CACHE_HEADERS)


def _is_reserved_server_path(path: str) -> bool:
    return path == "health" or path == "ws" or path.startswith("ws/") or path.startswith("api/")


def _current_extension_page(registry: Any, claims: JsonObject) -> tuple[Any | None, Any | None]:
    """Resolve the exact current page identity carried by a Run capability."""
    from core.extensions import ExtensionRegistrationIdentity

    extension = claims.get("extension")
    page = claims.get("page")
    epoch = claims.get("epoch")
    if not all(isinstance(value, str) and value for value in (extension, page, epoch)):
        return None, None
    identity = ExtensionRegistrationIdentity(cast(str, extension), cast(str, epoch))
    if not registry.is_registration_current(identity):
        return None, None
    for candidate, declaration, _entry in registry.page_declarations():
        if candidate == identity and declaration.page_id == page:
            return identity, declaration
    return None, None


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
