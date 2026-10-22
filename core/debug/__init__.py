"""Debug public API; timing observers do not load HTTP capture or its workers."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.debug.recorder import DebugContext, ProviderDebugRecorder
    from core.debug.redaction import redact_headers, redact_url
    from core.debug.store import DebugTraceStore, InvalidTraceIdError, drain_debug_traces

__all__ = [
    "DebugContext",
    "DebugTraceStore",
    "InvalidTraceIdError",
    "ProviderDebugRecorder",
    "drain_debug_traces",
    "redact_headers",
    "redact_url",
]

_EXPORT_MODULES = {
    "DebugContext": "recorder",
    "ProviderDebugRecorder": "recorder",
    "DebugTraceStore": "store",
    "InvalidTraceIdError": "store",
    "drain_debug_traces": "store",
    "redact_headers": "redaction",
    "redact_url": "redaction",
}


def __getattr__(name: str) -> Any:
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value
