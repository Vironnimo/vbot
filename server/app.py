"""FastAPI application factory for the vBot server layer."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
    from fastapi.responses import StreamingResponse  # type: ignore[import-not-found]
    from starlette.websockets import WebSocketDisconnect  # type: ignore[import-not-found]
except ModuleNotFoundError as exc:  # pragma: no cover - exercised when server extra is absent.
    _FASTAPI_IMPORT_ERROR = exc
    FastAPI = None  # type: ignore[assignment,misc]
    HTTPException = Any  # type: ignore[misc,assignment]
    Request = Any  # type: ignore[misc,assignment]
    StreamingResponse = Any  # type: ignore[misc,assignment]
    WebSocket = Any  # type: ignore[misc,assignment]
    WebSocketDisconnect = Exception  # type: ignore[misc,assignment]
else:
    _FASTAPI_IMPORT_ERROR = None

if TYPE_CHECKING:
    from fastapi import FastAPI as FastAPIType  # type: ignore[import-not-found]
else:
    FastAPIType = Any


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
        app.state.runtime = app_runtime
        app.state.chat_runs = ChatRunManager()
        app.state.chat_loop = ChatLoop(app_runtime)
        app.state.event_bus = ServerEventBus()
        _attach_run_manager(app_runtime, app.state.chat_runs)
        try:
            yield
        finally:
            app_runtime.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.runtime = app_runtime
    app.state.chat_runs = ChatRunManager()
    app.state.chat_loop = ChatLoop(app_runtime)
    app.state.event_bus = ServerEventBus()
    _attach_run_manager(app_runtime, app.state.chat_runs)

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

    return app


def _attach_run_manager(runtime: Any, run_manager: ChatRunManager) -> None:
    runtime.chat_runs = run_manager


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
