"""Voice bridge vocabulary, event publication and control failures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from desktop.speech.cues import (
    CUE_CANCEL,
    CUE_DONE,
    CUE_ERROR,
    CUE_FAILED,
    CUE_LISTEN,
    CUE_NO_SPEECH,
)

MODE_REAL = "real"
MODE_MOCK = "mock"
MODE_UNAVAILABLE = "unavailable"

STATE_OFF = "off"
STATE_STARTING = "starting"
STATE_LISTENING = "listening"
STATE_MICROPHONE_DISCONNECTED = "microphone_disconnected"
STATE_ERROR = "error"

ERROR_VOICE_STACK_UNAVAILABLE = "voice_stack_unavailable"
ERROR_MISSING_TARGET_AGENT = "missing_target_agent"
ERROR_ENGINE_START_FAILED = "engine_start_failed"
ERROR_PIPELINE_FAILED = "pipeline_failed"
ERROR_CALIBRATION_UNAVAILABLE = "calibration_unavailable"
ERROR_CALIBRATION_INACTIVE = "calibration_inactive"
ERROR_WAKEWORD_MODEL_ACTIVE = "wakeword_model_active"

EVENT_DETECTED = "detected"
EVENT_RECORDING_STARTED = "recording_started"
EVENT_RECORDING_ENDED = "recording_ended"
EVENT_NO_SPEECH = "no_speech"
EVENT_CANCELLED = "cancelled"
EVENT_TRANSCRIPTION_FAILED = "transcription_failed"
EVENT_COMMAND_FAILED = "command_failed"
EVENT_SENT = "sent"
EVENT_LIVE_REQUESTED = "live_requested"
EVENT_MICROPHONE_DISCONNECTED = "microphone_disconnected"
EVENT_MICROPHONE_RECONNECTED = "microphone_reconnected"
EVENT_ERROR = "error"

_EVENT_CUES = {
    EVENT_DETECTED: CUE_LISTEN,
    EVENT_SENT: CUE_DONE,
    EVENT_CANCELLED: CUE_CANCEL,
    EVENT_NO_SPEECH: CUE_NO_SPEECH,
    EVENT_TRANSCRIPTION_FAILED: CUE_FAILED,
    EVENT_COMMAND_FAILED: CUE_FAILED,
    EVENT_ERROR: CUE_ERROR,
}

LIVE_REQUEST_SOURCE = "wakeword"

READINESS_STALE_SECONDS = 300.0
"""A successful readiness check older than this is repeated on the next detection."""

CALIBRATION_PUSH_INTERVAL_SECONDS = 0.2


class VoiceEventSink(Protocol):
    """Receives Voice status snapshots and events for the page; never blocks."""

    def publish_status(self, status: Mapping[str, Any]) -> None: ...

    def publish_event(self, event: Mapping[str, Any]) -> None: ...


class VoiceControlError(RuntimeError):
    """A Voice request that cannot run in the current state, with a stable code."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class _Recording:
    command_id: str
    model_id: str
    agent_id: str | None
    session_behavior: str
