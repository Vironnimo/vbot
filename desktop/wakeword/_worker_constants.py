"""Worker constants."""

from __future__ import annotations

import logging
import threading

import httpx

_NO_SPEECH_DETECTOR_YET = object()
"""Marker separating "detector not yet resolved" from "resolved fail-open None"."""

logger = logging.getLogger("vbot.desktop.wakeword.worker")

_FRAME_SIZE_SAMPLES = 1280  # 80ms at 16kHz
_SAMPLE_RATE = 16000
_SAMPLE_WIDTH = 2  # 16-bit
_CHANNELS = 1

# Speech endpointing constants. The neural VAD (Silero, ONNX) consumes strict
# 512-sample hops at 16 kHz with a 64-sample leading context, so the recording
# loop reads 512-sample (32 ms) frames and each frame is exactly one hop. The
# detector buffers partial hops for callers feeding other chunk sizes. WebRTC
# VAD remains only as the fail-open fallback when the model cannot load.
_SPEECH_VAD_HOP_SAMPLES = 512  # 32 ms — Silero v5's fixed inference hop
_SPEECH_VAD_CONTEXT_SAMPLES = 64
_SPEECH_VAD_SAMPLE_RATE = 16000
_SPEECH_PROB_THRESHOLD = 0.5  # Silero's canonical speech threshold
_SPEECH_PROB_NEG_THRESHOLD = 0.35  # exit threshold (threshold - 0.15)
_VAD_MODE = 1  # Moderate aggressiveness (fallback paths remain WebRTC-based)
_VAD_FRAME_DURATION_MS = 32
_VAD_FRAME_SIZE = int(_SAMPLE_RATE * _VAD_FRAME_DURATION_MS / 1000)  # 512 samples

# The legacy WebRTC detection gate slices each 80 ms detection chunk into
# 10 ms frames; two speech slices (20 ms) open the gate so isolated blips
# cannot, while real speech beginning mid-chunk still passes. Only used when
# the neural speech detector is unavailable.
_DETECTION_VAD_FRAME_BYTES = int(_SAMPLE_RATE * 0.010) * _SAMPLE_WIDTH  # 320 bytes
_DETECTION_VAD_MIN_SPEECH_FRAMES = 2

_SILENCE_DURATION_SECONDS = 1.0
_SILENCE_FRAME_COUNT = int(_SILENCE_DURATION_SECONDS / (_VAD_FRAME_DURATION_MS / 1000))
_SPEECH_START_TIMEOUT_SECONDS = 1.5
_SPEECH_START_FRAME_COUNT = int(_SPEECH_START_TIMEOUT_SECONDS / (_VAD_FRAME_DURATION_MS / 1000))
_PRE_SPEECH_DURATION_SECONDS = 0.36  # covers a full 0.32 s detection pre-roll at 32 ms frames
_PRE_SPEECH_FRAME_COUNT = int(_PRE_SPEECH_DURATION_SECONDS / (_VAD_FRAME_DURATION_MS / 1000))
_DETECTION_PRE_ROLL_SECONDS = 0.32
_DETECTION_PRE_ROLL_CHUNKS = int(_DETECTION_PRE_ROLL_SECONDS / (_FRAME_SIZE_SAMPLES / _SAMPLE_RATE))

# Speech endpointing closes the recording itself; there is no fixed duration
# cap. The only recording stop besides silence and worker shutdown is the
# upload budget — the active ceiling the server enforces on every speech
# upload — so a user may speak as long as the server would still accept the
# audio. The budget is resolved once per worker (lazily, before the first
# recording) via settings.get_path and falls back to the mirrored default
# limit when the read fails, keeping a soft anti-runaway guard.
_SPEECH_UPLOAD_LIMIT_SAFETY_MARGIN_FRACTION = 0.9  # headroom for container overhead
_UPLOAD_BUDGET_FALLBACK_BYTES = 104_857_600  # mirrors DEFAULT_SPEECH_UPLOAD_MAX_SIZE_BYTES
_UPLOAD_BUDGET_SETTING_PATH = "speech.upload_max_size_bytes"
# The wave container adds a canonical 44-byte header before the PCM data.
_WAV_HEADER_BYTES = 44
_MAX_CONSECUTIVE_MIC_READ_ERRORS = 3
_MAX_RESAMPLER_FILL_READS = 16
_MICROPHONE_RECONNECT_INTERVAL_SECONDS = 30.0
_POST_DETECTION_LISTENING_HOLD_SECONDS = 1.0
_INTERRUPTIBLE_SLEEP_SLICE_SECONDS = 0.05

# Local STT may download model weights on first use. Keep connection/upload
# failures bounded separately from the longer inference response wait.
_HTTP_TIMEOUT = httpx.Timeout(600.0, connect=10.0, write=30.0, pool=10.0)
_RPC_TIMEOUT = 10.0
_MAX_RETRIES = 3

# Mock worker cadence. It walks the same detection→send state cycle the real
# worker does — driven by a MockWakewordEngine, no audio hardware or network —
# so the WebUI status indicator can be validated with --mock-wakeword.
_MOCK_FRAME_SECONDS = 0.1
_MOCK_STAGE_SECONDS = 0.8
# Idle low scores then a spike, so the mock periodically triggers one full cycle.
_MOCK_DEFAULT_SCORES = [0.0] * 25 + [1.0]

# Mirrors the always-retryable set in core/utils/http_status.py for a
# non-idempotent POST (audio transcription). Duplicated, not imported: the
# desktop process must not import from core (see .vorch/PROJECT.md).
_RETRYABLE_STATUS_CODES = frozenset([429, 502, 503, 504])
# Only RPC reads may be repeated after an ambiguous transport failure. Retrying
# session.create or chat.stream can duplicate a committed Session or Run when
# the server handled the first request but its response was lost.
_RETRYABLE_RPC_METHODS = frozenset(["agent.get", "session.list", "settings.get_path"])

_VOICE_CANCEL_PHRASES = frozenset(["abbrechen", "vergiss es"])
_COMMON_CAPTURE_SAMPLE_RATES = (16000, 48000, 44100, 32000)
_CAPTURE_DTYPES = ("int16", "float32")
_AUDIO_BACKEND_LOCK = threading.Lock()

_OUTCOME_SENT = "sent"
_OUTCOME_CANCELLED = "cancelled"
_OUTCOME_NO_SPEECH = "no_speech"
_OUTCOME_TRANSCRIPTION_FAILED = "transcription_failed"
_TASK_SPEECH_TO_TEXT = "speech_to_text"
_TASK_MODEL_STATUS_METHOD = "task_model.status"

_ERROR_NO_SERVER = "no_server"
_ERROR_SERVER_UNREACHABLE = "server_unreachable"
_ERROR_SPEECH_TO_TEXT_UNCONFIGURED = "speech_to_text_unconfigured"
_ERROR_SPEECH_TO_TEXT_UNAVAILABLE = "speech_to_text_unavailable"
_ERROR_SPEECH_TO_TEXT_READINESS_FAILED = "speech_to_text_readiness_failed"
