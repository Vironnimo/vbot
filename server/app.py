"""FastAPI application factory for the vBot server layer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.chat import ChatLoop, ChatRunManager, RunNotFoundError
from core.runtime import Runtime
from core.utils.config import Config
from server.delegates import dispatch_rpc
from server.events import ServerEventBus

JsonObject = dict[str, Any]
_FASTAPI_IMPORT_ERROR: ModuleNotFoundError | None

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket  # type: ignore[import-not-found]
    from fastapi.responses import FileResponse, StreamingResponse  # type: ignore[import-not-found]
    from fastapi.staticfiles import StaticFiles  # type: ignore[import-not-found]
    from starlette.websockets import WebSocketDisconnect  # type: ignore[import-not-found]
except ModuleNotFoundError as exc:  # pragma: no cover - exercised when server extra is absent.
    _FASTAPI_IMPORT_ERROR = exc
    FastAPI = None  # type: ignore[assignment,misc]
    FileResponse = Any  # type: ignore[misc,assignment]
    HTTPException = Any  # type: ignore[misc,assignment]
    Request = Any  # type: ignore[misc,assignment]
    StaticFiles = Any  # type: ignore[misc,assignment]
    StreamingResponse = Any  # type: ignore[misc,assignment]
    WebSocket = Any  # type: ignore[misc,assignment]
    WebSocketDisconnect = Exception  # type: ignore[misc,assignment]
else:
    _FASTAPI_IMPORT_ERROR = None

if TYPE_CHECKING:
    from fastapi import FastAPI as FastAPIType  # type: ignore[import-not-found]
else:
    FastAPIType = Any

WEBUI_DIST_DIR = Path(__file__).resolve().parents[1] / "webui" / "dist"


def create_app(*, runtime: Runtime | None = None, config: Config | None = None) -> FastAPIType:
    """Create the FastAPI app and wire runtime services into app state."""
    if FastAPI is None:
        raise RuntimeError(
            "FastAPI is required to create the server app"
        ) from _FASTAPI_IMPORT_ERROR
    app_runtime = runtime or Runtime(config or Config())

    @asynccontextmanager
    async def lifespan(app: FastAPIType) -> AsyncIterator[None]:
        app_runtime.start()
        _initialize_app_state(app, app_runtime)
        try:
            yield
        finally:
            app_runtime.stop()

    app = FastAPI(lifespan=lifespan)
    _initialize_app_state(app, app_runtime)

    @app.get("/health")
    async def health() -> JsonObject:
        return {"status": "ok"}

    @app.post("/api/rpc")
    async def rpc(request: Request) -> JsonObject:
        payload = await request.json()
        return await dispatch_rpc(request.app.state, payload)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str) -> StreamingResponse:
        try:
            run = request.app.state.chat_runs.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return StreamingResponse(
            _sse_run_events(run),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.websocket("/ws")
    async def websocket_events(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            async for event in websocket.app.state.event_bus.subscribe():
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return

    _mount_webui(app)

    return app


def _initialize_app_state(app: FastAPIType, runtime: Runtime) -> None:
    app.state.runtime = runtime
    app.state.chat_runs = ChatRunManager()
    app.state.chat_loop = ChatLoop(runtime)
    app.state.event_bus = ServerEventBus()
    app.state.agent_delete_lock = asyncio.Lock()
    _attach_run_manager(runtime, app.state.chat_runs)


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


def _attach_run_manager(runtime: Any, run_manager: ChatRunManager) -> None:
    runtime.chat_runs = run_manager


def _is_reserved_server_path(path: str) -> bool:
    return path == "health" or path == "ws" or path.startswith("api/")


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


async def _sse_run_events(run: Any) -> AsyncIterator[str]:
    async for event in run.subscribe():
        data = _remove_opaque_provider_metadata(event.to_dict())
        yield f"event: {event.type}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


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
