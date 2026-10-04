"""The offline wire: a recording mock transport and the patches that route all traffic to it.

Every Provider HTTP client is built by ``core.providers._http_shared.build_async_client``;
the snapshot replaces that builder with one whose client uses an
``httpx.MockTransport`` bound to :class:`WireRecorder`. Bare ``httpx`` clients
(the OAuth token exchange) reach the same recorder through the default async
transport, the Codex WebSocket connector fails fast so the production SSE
fallback carries the request, and name resolution plus synchronous ``httpx``
transports raise. Nothing can reach the network while :func:`offline_wire` is
active.
"""

from __future__ import annotations

import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import httpx
import websockets.asyncio.client

import core.providers._http_shared as http_shared
from scripts._wire_snapshot.canned import canned_chat_response, protocol_for_path
from scripts._wire_snapshot.records import Masker

type RecorderMode = Literal["capture", "respond", "setup"]

LMSTUDIO_MODELS_PATH = "/api/v1/models"
LMSTUDIO_LOAD_PATH = "/api/v1/models/load"
COPILOT_TOKEN_PATH = "/copilot_internal/v2/token"
COPILOT_API_ENDPOINT = "https://api.individual.githubcopilot.com"
COPILOT_API_TOKEN = "snapshot-copilot-api-token"
_FAR_FUTURE_EPOCH = 4102444800  # 2100-01-01T00:00:00Z


class CaptureSentinel(BaseException):
    """Raised by the mock transport right after it records a chat request.

    A ``BaseException`` so Adapter retry, sampling-fallback and transport
    wrappers, which handle ``Exception``, can never mistake it for a Provider
    failure; a render whose sentinel does not come back out was swallowed.
    """


class WireRecorder:
    """Record every request of one render and answer it offline.

    In ``capture`` mode the first chat request is recorded and answered by
    raising :class:`CaptureSentinel`. In ``respond`` mode it is answered with
    the canned response of its wire protocol. Preparatory requests (LM Studio
    model load, Copilot token exchange) are always answered with canned
    payloads and recorded too.
    """

    def __init__(self, masker: Masker) -> None:
        self.masker = masker
        self.mode: RecorderMode = "setup"
        self.model_id = ""
        self.requests: list[dict[str, Any]] = []
        self.chat_requests = 0
        self.chat_protocol: str | None = None

    def begin(self, mode: RecorderMode, model_id: str = "") -> None:
        """Start recording one render."""

        self.mode = mode
        self.model_id = model_id
        self.requests = []
        self.chat_requests = 0
        self.chat_protocol = None

    def take(self) -> list[dict[str, Any]]:
        """Return and clear the requests recorded since :meth:`begin`."""

        requests, self.requests = self.requests, []
        return requests

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one HTTP request (the ``httpx.MockTransport`` handler)."""

        self.requests.append(self.masker.request(request))
        path = request.url.path
        if request.method == "GET" and path.endswith(LMSTUDIO_MODELS_PATH):
            return httpx.Response(
                200, json={"models": [{"key": self.model_id, "loaded_instances": []}]}
            )
        if request.method == "POST" and path.endswith(LMSTUDIO_LOAD_PATH):
            return httpx.Response(200, json={"type": "llm", "status": "loaded"})
        if request.method == "GET" and path.endswith(COPILOT_TOKEN_PATH):
            return httpx.Response(
                200,
                json={
                    "token": COPILOT_API_TOKEN,
                    "expires_at": _FAR_FUTURE_EPOCH,
                    "refresh_in": 1500,
                    "endpoints": {"api": COPILOT_API_ENDPOINT},
                },
            )
        if request.method != "POST":
            return httpx.Response(404, json={"error": {"message": "snapshot: unexpected request"}})
        self.chat_requests += 1
        self.chat_protocol = protocol_for_path(path)
        if self.mode == "capture":
            raise CaptureSentinel
        return canned_chat_response(request)

    def websocket_connect(self, uri: str, **kwargs: Any) -> Any:
        """Record a WebSocket connection attempt and refuse it."""

        headers = kwargs.get("additional_headers") or {}
        items = headers.items() if hasattr(headers, "items") else headers
        self.requests.append(
            {
                "method": "WEBSOCKET",
                "url": self.masker.text(uri),
                "headers": self.masker.headers(items),
                "body": None,
            }
        )
        raise OSError("snapshot: WebSocket transport is disabled")


def _network_blocked(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("snapshot: network access is blocked")


def _replace_module_attributes(original: object, replacement: object) -> list[tuple[Any, str]]:
    """Point every loaded ``core`` module attribute bound to *original* at *replacement*."""

    replaced: list[tuple[Any, str]] = []
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "core" or name.startswith("core.")):
            continue
        for attribute, value in list(vars(module).items()):
            if value is original:
                setattr(module, attribute, replacement)
                replaced.append((module, attribute))
    return replaced


@contextmanager
def offline_wire(recorder: WireRecorder) -> Iterator[None]:
    """Route all Provider traffic to *recorder* and block the network.

    Import every Adapter module before entering, so their module-level
    ``build_async_client`` and WebSocket ``connect`` bindings exist to patch.
    """

    original_builder = http_shared.build_async_client
    original_connect = websockets.asyncio.client.connect
    original_async_transport = httpx.AsyncHTTPTransport.handle_async_request
    original_sync_transport = httpx.HTTPTransport.handle_request
    original_getaddrinfo = socket.getaddrinfo
    original_create_connection = socket.create_connection

    def build_async_client(
        *,
        base_url: str,
        timeout: httpx.Timeout | None = None,
        debug_recorder: Any = None,
    ) -> httpx.AsyncClient:
        del timeout, debug_recorder
        return httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(30.0),
            transport=httpx.MockTransport(recorder.handle),
        )

    async def handle_async_request(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        del self
        await request.aread()
        return recorder.handle(request)

    patched = _replace_module_attributes(original_builder, build_async_client)
    patched += _replace_module_attributes(original_connect, recorder.websocket_connect)
    httpx.AsyncHTTPTransport.handle_async_request = handle_async_request  # type: ignore[method-assign]
    httpx.HTTPTransport.handle_request = _network_blocked  # type: ignore[method-assign]
    socket.getaddrinfo = _network_blocked
    socket.create_connection = _network_blocked
    try:
        yield
    finally:
        socket.create_connection = original_create_connection
        socket.getaddrinfo = original_getaddrinfo
        httpx.HTTPTransport.handle_request = original_sync_transport  # type: ignore[method-assign]
        httpx.AsyncHTTPTransport.handle_async_request = original_async_transport  # type: ignore[method-assign]
        for module, attribute in patched:
            current = getattr(module, attribute)
            setattr(
                module,
                attribute,
                original_builder if current is build_async_client else original_connect,
            )
