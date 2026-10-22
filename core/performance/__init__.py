"""Performance measurement public API.

Exports load on first access, so importing a light member such as an option bound
does not import the measurement stack and its process-wide sink.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.performance._limits import (
        DEFAULT_HEAP_TOP,
        DEFAULT_HISTORY_WINDOWS,
        DEFAULT_RECORDING_SECONDS,
        HISTORY_INTERVAL_SECONDS,
        HISTORY_RETENTION_DAYS,
        MAX_HEAP_TOP,
        MAX_HISTORY_NAMES,
        MAX_HISTORY_WINDOWS,
        MAX_LABEL_LENGTH,
        MAX_RECORDING_SECONDS,
        RETAINED_RECORDINGS,
    )
    from core.performance.performance import (
        ExternalHistogram,
        Measurement,
        PerformanceError,
        PerformanceService,
        RecordingActiveError,
        RecordingInactiveError,
        count,
        measure,
        merge_histogram,
        record_duration,
        record_span,
        session_track,
        set_gauge,
    )


_EXPORT_MODULES = {
    "DEFAULT_HEAP_TOP": "core.performance._limits",
    "DEFAULT_HISTORY_WINDOWS": "core.performance._limits",
    "DEFAULT_RECORDING_SECONDS": "core.performance._limits",
    "HISTORY_INTERVAL_SECONDS": "core.performance._limits",
    "HISTORY_RETENTION_DAYS": "core.performance._limits",
    "MAX_HEAP_TOP": "core.performance._limits",
    "MAX_HISTORY_NAMES": "core.performance._limits",
    "MAX_HISTORY_WINDOWS": "core.performance._limits",
    "MAX_LABEL_LENGTH": "core.performance._limits",
    "MAX_RECORDING_SECONDS": "core.performance._limits",
    "RETAINED_RECORDINGS": "core.performance._limits",
    "ExternalHistogram": "core.performance.performance",
    "Measurement": "core.performance.performance",
    "PerformanceError": "core.performance.performance",
    "PerformanceService": "core.performance.performance",
    "RecordingActiveError": "core.performance.performance",
    "RecordingInactiveError": "core.performance.performance",
    "count": "core.performance.performance",
    "measure": "core.performance.performance",
    "merge_histogram": "core.performance.performance",
    "record_duration": "core.performance.performance",
    "record_span": "core.performance.performance",
    "session_track": "core.performance.performance",
    "set_gauge": "core.performance.performance",
}


def __getattr__(name: str) -> Any:
    """Load public performance exports only when a caller requests them."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "DEFAULT_HEAP_TOP",
    "DEFAULT_HISTORY_WINDOWS",
    "DEFAULT_RECORDING_SECONDS",
    "HISTORY_INTERVAL_SECONDS",
    "HISTORY_RETENTION_DAYS",
    "MAX_HEAP_TOP",
    "MAX_HISTORY_NAMES",
    "MAX_HISTORY_WINDOWS",
    "MAX_LABEL_LENGTH",
    "MAX_RECORDING_SECONDS",
    "RETAINED_RECORDINGS",
    "ExternalHistogram",
    "Measurement",
    "PerformanceError",
    "PerformanceService",
    "RecordingActiveError",
    "RecordingInactiveError",
    "count",
    "measure",
    "merge_histogram",
    "record_duration",
    "record_span",
    "session_track",
    "set_gauge",
]
