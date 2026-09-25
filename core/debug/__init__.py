"""Debug mode public API — provider wire trace storage, recording, and redaction."""

from core.debug.recorder import DebugContext, ProviderDebugRecorder
from core.debug.redaction import redact_headers, redact_json_body, redact_url
from core.debug.store import DebugTraceStore, InvalidTraceIdError, drain_debug_traces

__all__ = [
    "DebugContext",
    "DebugTraceStore",
    "InvalidTraceIdError",
    "ProviderDebugRecorder",
    "drain_debug_traces",
    "redact_headers",
    "redact_json_body",
    "redact_url",
]
