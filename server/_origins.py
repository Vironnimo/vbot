"""Fail-closed browser Origin policy for HTTP and WebSocket transports."""

from __future__ import annotations

import ipaddress
from collections.abc import MutableMapping
from typing import Any, cast
from urllib.parse import SplitResult, urlsplit

from server._bind import ServerBindState
from server._http_dependencies import Response
from server.file_delivery import EXTENSION_ASSET_URL_PREFIX, PREVIEW_URL_PREFIX

JsonObject = dict[str, Any]

HTTP_ORIGIN_REJECTED_STATUS_CODE = 403

WEBSOCKET_POLICY_VIOLATION_CODE = 1008

HTTP_ORIGIN_SCHEMES = frozenset({"http", "https"})

ORIGIN_HEADER_NAME = b"origin"

HOST_HEADER_NAME = b"host"


class _BrowserOriginGuardMiddleware:
    """Reject browser transports whose Origin is not a configured server origin."""

    def __init__(
        self,
        app: Any,
        *,
        allowed_origins: frozenset[tuple[str, str, int]],
        same_origin_ip_port: int | None,
    ) -> None:
        self._app = app
        self._allowed_origins = allowed_origins
        self._same_origin_ip_port = same_origin_ip_port

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        # Sandboxed documents have an opaque Origin. Only read-only preview
        # asset routes admit it; their handler still validates the capability.
        if (
            scope_type == "http"
            and scope.get("method") in {"GET", "HEAD"}
            and str(scope.get("path", "")).startswith(
                (PREVIEW_URL_PREFIX, EXTENSION_ASSET_URL_PREFIX)
            )
            and _scope_header_values(scope, ORIGIN_HEADER_NAME) == ["null"]
        ):
            await self._app(scope, receive, send)
            return
        if scope_type not in {"http", "websocket"} or _scope_has_allowed_origin(
            scope,
            self._allowed_origins,
            same_origin_ip_port=self._same_origin_ip_port,
        ):
            await self._app(scope, receive, send)
            return
        if scope_type == "http":
            response = Response(status_code=HTTP_ORIGIN_REJECTED_STATUS_CODE)
            await response(scope, receive, send)
            return
        await send(
            {
                "type": "websocket.close",
                "code": WEBSOCKET_POLICY_VIOLATION_CODE,
                "reason": "Cross-origin WebSocket connections are forbidden",
            }
        )


def _scope_has_allowed_origin(
    scope: MutableMapping[str, Any],
    allowed_origins: frozenset[tuple[str, str, int]],
    *,
    same_origin_ip_port: int | None,
) -> bool:
    origin_values = _scope_header_values(scope, ORIGIN_HEADER_NAME)
    if not origin_values:
        return True
    if len(origin_values) != 1:
        return False
    origin = _parse_origin(origin_values[0])
    if origin is None:
        return False
    if origin in allowed_origins:
        return True
    if (
        same_origin_ip_port is None
        or origin[2] != same_origin_ip_port
        or not _is_ip_literal(origin[1])
    ):
        return False
    target_scheme = _http_scheme(scope.get("scheme"))
    host_values = _scope_header_values(scope, HOST_HEADER_NAME)
    if target_scheme is None or len(host_values) != 1:
        return False
    target = _parse_origin(f"{target_scheme}://{host_values[0]}")
    return target is not None and origin == target


def _scope_header_values(scope: MutableMapping[str, Any], name: bytes) -> list[str]:
    headers = scope.get("headers")
    if not isinstance(headers, list):
        return []
    return [
        value.decode("latin-1")
        for key, value in headers
        if isinstance(key, bytes) and isinstance(value, bytes) and key.lower() == name
    ]


def _http_scheme(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return {"http": "http", "https": "https", "ws": "http", "wss": "https"}.get(value.casefold())


def _parse_origin(value: str) -> tuple[str, str, int] | None:
    if value == "null":
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if not _is_serialized_origin(parsed):
        return None
    scheme = parsed.scheme.casefold()
    default_port = 443 if scheme == "https" else 80
    effective_port = default_port if port is None else port
    return scheme, cast(str, parsed.hostname).casefold(), effective_port


def _configured_browser_origins(server_bind: ServerBindState) -> frozenset[tuple[str, str, int]]:
    """Return browser origins directly bound by this server, never request headers."""
    host = server_bind["listen_host"]
    if host in {"0.0.0.0", "::"}:
        return frozenset()
    origin = _parse_origin(f"http://{_format_origin_host(host)}:{server_bind['listen_port']}")
    if origin is None:
        raise RuntimeError(f"Invalid server bind host for browser origin guard: {host!r}")
    return frozenset({origin})


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _format_origin_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _is_serialized_origin(parsed: SplitResult) -> bool:
    return (
        parsed.scheme.casefold() in HTTP_ORIGIN_SCHEMES
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )
