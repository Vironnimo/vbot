"""Native Desktop Extension contract; importing this module loads no server stack."""

from typing import Any

from ._exports import resolve

_EXPORTS = dict.fromkeys(
    (
        "DESKTOP_EXTENSION_API_VERSION",
        "DesktopExtensionAPI",
        "ExecutionHandler",
        "EventSink",
        "HandlerFactory",
        "NativeCapability",
    ),
    "desktop.extensions",
)


def __getattr__(name: str) -> Any:
    value = resolve(__name__, _EXPORTS, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = list(_EXPORTS)
