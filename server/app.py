"""FastAPI application factory for the vBot server layer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from core.chat import ChatLoop, ChatRunManager
from core.runtime import Runtime
from core.utils.config import Config
from server.delegates import dispatch_rpc

JsonObject = dict[str, Any]
_FASTAPI_IMPORT_ERROR: ModuleNotFoundError | None

try:
    from fastapi import FastAPI, Request  # type: ignore[import-not-found]
except ModuleNotFoundError as exc:  # pragma: no cover - exercised when server extra is absent.
    _FASTAPI_IMPORT_ERROR = exc
    FastAPI = None  # type: ignore[assignment,misc]
    Request = Any  # type: ignore[misc,assignment]
else:
    _FASTAPI_IMPORT_ERROR = None

if TYPE_CHECKING:
    from fastapi import FastAPI as FastAPIType  # type: ignore[import-not-found]
else:
    FastAPIType = Any


class ServerEventBus:
    """Placeholder event bus for server lifecycle events."""

    def __init__(self) -> None:
        self.events: list[JsonObject] = []

    def publish(self, event_type: str, payload: JsonObject | None = None) -> None:
        """Record a server event for later transport integration."""
        self.events.append({"type": event_type, "payload": dict(payload or {})})


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

    return app


def _attach_run_manager(runtime: Any, run_manager: ChatRunManager) -> None:
    runtime.chat_runs = run_manager
