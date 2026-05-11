"""Tests for server delegate run event routing constants."""

from core.chat.runs import TOOL_CALL_STDERR_EVENT, TOOL_CALL_STDOUT_EVENT
from server.delegates import RUN_DELTA_EVENT_TYPES, SERVER_EVENT_TYPES
from server.events import ALLOWED_SERVER_EVENT_TYPES


def test_process_output_deltas_are_sse_only_not_websocket_events() -> None:
    """Process stdout/stderr deltas stream over SSE and do not bridge to WebSocket."""
    process_delta_events = {TOOL_CALL_STDOUT_EVENT, TOOL_CALL_STDERR_EVENT}

    assert process_delta_events <= RUN_DELTA_EVENT_TYPES
    assert process_delta_events.isdisjoint(SERVER_EVENT_TYPES)
    assert process_delta_events.isdisjoint(ALLOWED_SERVER_EVENT_TYPES)
