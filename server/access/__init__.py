"""Access control at the server edge: access guard, pairing, devices and TLS.

``core.access`` owns the access configuration and the device store; this package
applies them to HTTP and WebSocket requests. See the ``access`` domain map.
"""

from server.access._guard import (
    WEBSOCKET_ACCESS_DENIED,
    WEBSOCKET_AUTH_REQUIRED,
    AccessGuard,
)
from server.access._principal import Principal, current_principal
from server.access._routes import register_access_routes
from server.access._service import (
    ACCESS_DENIED,
    AUTH_DISABLED,
    AUTH_REQUIRED,
    DEVICE_COOKIE,
    DEVICE_NOT_FOUND,
    DEVICES_UNAVAILABLE,
    PAIRING_NOT_FOUND,
    RATE_LIMITED,
    AccessError,
    AccessService,
)

__all__ = [
    "ACCESS_DENIED",
    "AUTH_DISABLED",
    "AUTH_REQUIRED",
    "DEVICES_UNAVAILABLE",
    "DEVICE_COOKIE",
    "DEVICE_NOT_FOUND",
    "PAIRING_NOT_FOUND",
    "RATE_LIMITED",
    "WEBSOCKET_ACCESS_DENIED",
    "WEBSOCKET_AUTH_REQUIRED",
    "AccessError",
    "AccessGuard",
    "AccessService",
    "Principal",
    "current_principal",
    "register_access_routes",
]
