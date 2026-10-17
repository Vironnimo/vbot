"""The access guard: the first ASGI middleware, for every HTTP request and WebSocket."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from server.access._principal import PRINCIPAL_SCOPE_KEY, Principal
from server.access._rules import RouteClass, classify_route, request_host
from server.access._service import (
    ACCESS_DENIED,
    AUTH_REQUIRED,
    DEVICE_COOKIE,
    RATE_LIMITED,
    AccessService,
)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

#: WebSocket close codes accessors act on: stop reconnecting, show pairing or refusal.
WEBSOCKET_AUTH_REQUIRED = 4401
WEBSOCKET_ACCESS_DENIED = 4403

#: The WebUI shell's Content-Security-Policy. Styles stay inline-capable: Svelte
#: templates and xterm.js set style attributes. Images allow remote http(s) URLs
#: because Markdown shows remote images; everything active is same-origin.
WEBUI_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https: http:; media-src 'self' data: blob:; "
    "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; "
    "frame-src 'self'; manifest-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
)
HSTS_VALUE = b"max-age=31536000"
CLEAR_DEVICE_COOKIE = (
    f"{DEVICE_COOKIE}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict".encode("latin-1")
)


class AccessGuard:
    """Apply the access mode to every request before the Origin guard and routing.

    It resolves the effective client, refuses peers and transports the mode does
    not admit (403 ``access_denied``, WebSocket 4403), authenticates, refuses
    protected routes without a principal in ``authenticated`` mode (401
    ``auth_required``, WebSocket 4401, 429 ``rate_limited`` once a client keeps
    presenting bad credentials) and attaches the principal to the scope. A
    device's requests are tracked so that revoking the device ends its
    WebSockets and streams and refuses answers not yet started. Every HTTP
    response gets the security headers.
    """

    def __init__(self, app: Any, *, service: AccessService) -> None:
        self._app = app
        self._service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        service = self._service
        path = str(scope.get("path") or "")
        route = classify_route(str(scope_type), str(scope.get("method") or ""), path)
        facts = service.inspect(scope)
        if scope_type == "http":
            send = _with_security_headers(send, route=route, secure=facts.secure)
        if facts.refusal == "host_not_allowed":
            # Answering only to known names defeats DNS rebinding.
            service.refused(facts.refusal, facts, path, detail=f"host={request_host(scope)}")
            await _refuse(
                scope, receive, send, 403, ACCESS_DENIED, "Access denied: unknown host name"
            )
            return
        if facts.refusal is not None:
            service.refused(facts.refusal, facts, path)
            await _refuse(scope, receive, send, 403, ACCESS_DENIED, "Access denied")
            return
        authentication = service.authenticate(list(scope.get("headers") or ()), facts)
        wait = None
        if authentication.failure is not None:
            service.refused(authentication.failure, facts, path)
            wait = service.failed_authentication(facts)
        if authentication.clear_cookie:
            send = _with_cleared_cookie(send)
        principal = authentication.principal
        if route == "protected" and principal is None:
            if wait is not None:
                await _refuse(
                    scope,
                    receive,
                    send,
                    429,
                    RATE_LIMITED,
                    "Too many failed sign-in attempts",
                    retry_after=wait,
                )
            else:
                await _refuse(scope, receive, send, 401, AUTH_REQUIRED, "Authentication required")
            return
        scope[PRINCIPAL_SCOPE_KEY] = principal
        if principal is not None and principal.device is not None:
            await self._tracked(scope, receive, send, principal)
        else:
            await self._app(scope, receive, send)

    async def _tracked(
        self, scope: Scope, receive: Receive, send: Send, principal: Principal
    ) -> None:
        """Run a device's request so that revoking the device ends or refuses it.

        The request is registered from its first moment: revocation cancels a
        WebSocket or a started response, and a response that starts after the
        revocation becomes a 401 ``auth_required`` instead.
        """
        assert principal.device is not None
        connections = self._service.connections
        websocket = scope.get("type") == "websocket"
        state = {"accepted": False, "closed": False, "started": False, "finished": False}
        connection = connections.register(principal.device.id, streaming=websocket)

        async def tracked_send(message: Message) -> None:
            kind = message.get("type")
            if connection.revoked:
                if kind == "http.response.start" and not state["started"]:
                    state["started"] = state["finished"] = True
                    await _refuse(scope, receive, send, 401, AUTH_REQUIRED, "Device signed out")
                raise asyncio.CancelledError
            if kind == "websocket.accept":
                state["accepted"] = True
            elif kind == "websocket.close":
                state["closed"] = True
            elif kind == "http.response.start":
                state["started"] = True
                connection.streaming = True
            elif kind == "http.response.body" and not message.get("more_body", False):
                state["finished"] = True
                connection.streaming = False
            await send(message)

        async def tracked_receive() -> Message:
            if connection.revoked:
                return (
                    {"type": "websocket.disconnect", "code": WEBSOCKET_AUTH_REQUIRED}
                    if websocket
                    else {"type": "http.disconnect"}
                )
            return await receive()

        try:
            try:
                await self._app(scope, tracked_receive, tracked_send)
            finally:
                connections.unregister(connection)
        except asyncio.CancelledError:
            if not connection.revoked:
                raise
        if not connection.revoked:
            return
        connection.settle()
        try:
            if websocket:
                if not state["closed"]:
                    if not state["accepted"]:
                        await send({"type": "websocket.accept"})
                    await send(
                        {
                            "type": "websocket.close",
                            "code": WEBSOCKET_AUTH_REQUIRED,
                            "reason": "Device signed out",
                        }
                    )
            elif state["started"] and not state["finished"]:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except OSError, RuntimeError:
            # The client is already gone.
            pass


async def _refuse(
    scope: Scope,
    receive: Receive,
    send: Send,
    status: int,
    code: str,
    message: str,
    *,
    retry_after: float | None = None,
) -> None:
    if scope.get("type") == "websocket":
        # A close before accept reaches the client only as HTTP 403 without a
        # code, so the guard accepts and closes at once: the route never runs.
        await receive()
        await send({"type": "websocket.accept"})
        await send(
            {
                "type": "websocket.close",
                "code": WEBSOCKET_ACCESS_DENIED if status == 403 else WEBSOCKET_AUTH_REQUIRED,
                "reason": message,
            }
        )
        return
    body = json.dumps({"ok": False, "error": {"code": code, "message": message}}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    if retry_after is not None:
        headers.append((b"retry-after", str(max(1, round(retry_after))).encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _with_security_headers(send: Send, *, route: RouteClass, secure: bool) -> Send:
    async def secured(message: Message) -> None:
        if message.get("type") == "http.response.start":
            headers = list(message.get("headers") or ())
            names = {name.lower() for name, _value in headers}
            additions: list[tuple[bytes, bytes]] = [(b"x-content-type-options", b"nosniff")]
            if secure:
                additions.append((b"strict-transport-security", HSTS_VALUE))
            if route == "shell":
                additions.append((b"referrer-policy", b"same-origin"))
                additions.append(
                    (b"content-security-policy", WEBUI_CONTENT_SECURITY_POLICY.encode("ascii"))
                )
            headers.extend(item for item in additions if item[0] not in names)
            message = {**message, "headers": headers}
        await send(message)

    return secured


def _with_cleared_cookie(send: Send) -> Send:
    async def clearing(message: Message) -> None:
        if message.get("type") == "http.response.start":
            message = {
                **message,
                "headers": [*(message.get("headers") or ()), (b"set-cookie", CLEAR_DEVICE_COOKIE)],
            }
        await send(message)

    return clearing
