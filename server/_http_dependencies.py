"""Optional HTTP framework imports shared by the server transport implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
        RedirectResponse,
        Response,
        StreamingResponse,
    )
    from fastapi.staticfiles import StaticFiles  # type: ignore[import-not-found]
    from starlette.datastructures import (  # type: ignore[import-not-found]
        UploadFile as StarletteUploadFile,
    )
    from starlette.formparsers import (  # type: ignore[import-not-found]
        MultiPartException,
        MultiPartParser,
    )
    from starlette.websockets import WebSocketDisconnect  # type: ignore[import-not-found]
except ModuleNotFoundError as exc:  # pragma: no cover - exercised when server extra is absent.
    _FASTAPI_IMPORT_ERROR = exc
    FastAPI = None  # type: ignore[assignment,misc]
    RedirectResponse = Any  # type: ignore[misc,assignment]
    FileResponse = Any  # type: ignore[misc,assignment]
    HTTPException = Any  # type: ignore[misc,assignment]
    Request = Any  # type: ignore[misc,assignment]
    Response = Any  # type: ignore[misc,assignment]
    StaticFiles = Any  # type: ignore[misc,assignment]
    StarletteUploadFile = Any  # type: ignore[misc,assignment]
    StreamingResponse = Any  # type: ignore[misc,assignment]
    UploadFile = Any  # type: ignore[misc,assignment]
    WebSocket = Any  # type: ignore[misc,assignment]
    WebSocketDisconnect = Exception  # type: ignore[misc,assignment]
    MultiPartException = Exception  # type: ignore[misc,assignment]
    MultiPartParser = object  # type: ignore[misc,assignment]
else:
    _FASTAPI_IMPORT_ERROR = None


if TYPE_CHECKING:
    from fastapi import FastAPI as FastAPIType  # type: ignore[import-not-found]
else:
    FastAPIType = Any


__all__ = [
    "FastAPI",
    "HTTPException",
    "Request",
    "UploadFile",
    "WebSocket",
    "FileResponse",
    "RedirectResponse",
    "Response",
    "StreamingResponse",
    "StaticFiles",
    "StarletteUploadFile",
    "MultiPartException",
    "MultiPartParser",
    "WebSocketDisconnect",
    "FastAPIType",
    "_FASTAPI_IMPORT_ERROR",
]
