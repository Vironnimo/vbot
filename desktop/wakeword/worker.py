"""Wakeword worker thread — detection → recording → transcription → sending.

Runs in a daemon thread and publishes state transitions through the bridge
so the WebUI can show live status via poll-based `getWakewordStatus()`.
"""

from __future__ import annotations

import io
import logging
import random
import re
import threading
import time
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from desktop.wakeword.engine import MockWakewordEngine

logger = logging.getLogger("vbot.desktop.wakeword.worker")

_FRAME_SIZE_SAMPLES = 1280  # 80ms at 16kHz
_SAMPLE_RATE = 16000
_SAMPLE_WIDTH = 2  # 16-bit
_CHANNELS = 1

_VAD_MODE = 1  # Moderate aggressiveness
_VAD_FRAME_DURATION_MS = 30
_VAD_FRAME_SIZE = int(_SAMPLE_RATE * _VAD_FRAME_DURATION_MS / 1000)  # 480 samples

_SILENCE_DURATION_SECONDS = 1.5
_SILENCE_FRAME_COUNT = int(_SILENCE_DURATION_SECONDS / (_VAD_FRAME_DURATION_MS / 1000))
_SPEECH_START_TIMEOUT_SECONDS = 4.0
_SPEECH_START_FRAME_COUNT = int(_SPEECH_START_TIMEOUT_SECONDS / (_VAD_FRAME_DURATION_MS / 1000))
_PRE_SPEECH_DURATION_SECONDS = 0.3
_PRE_SPEECH_FRAME_COUNT = int(_PRE_SPEECH_DURATION_SECONDS / (_VAD_FRAME_DURATION_MS / 1000))
_DETECTION_PRE_ROLL_SECONDS = 0.32
_DETECTION_PRE_ROLL_CHUNKS = int(_DETECTION_PRE_ROLL_SECONDS / (_FRAME_SIZE_SAMPLES / _SAMPLE_RATE))

_MAX_RECORDING_SECONDS = 15.0
_MAX_RECORDING_FRAMES = int(_MAX_RECORDING_SECONDS / (_VAD_FRAME_DURATION_MS / 1000))
_MAX_CONSECUTIVE_MIC_READ_ERRORS = 3
_POST_DETECTION_LISTENING_HOLD_SECONDS = 1.0
_INTERRUPTIBLE_SLEEP_SLICE_SECONDS = 0.05

_HTTP_TIMEOUT = 30.0
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

_VOICE_CANCEL_PHRASES = frozenset(["abbrechen", "vergiss es"])
_COMMON_CAPTURE_SAMPLE_RATES = (16000, 48000, 44100, 32000)
_CAPTURE_DTYPES = ("int16", "float32")

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


class MicrophoneUnavailableError(RuntimeError):
    """No usable input-device format could supply wakeword-quality audio."""


@dataclass(frozen=True)
class CaptureFormat:
    """Concrete device format used before conversion to 16 kHz PCM."""

    device: int
    name: str
    sample_rate: int
    dtype: str


@dataclass(frozen=True)
class CapturedAudioFrame:
    """One microphone read projected for detection and command recording."""

    detection_pcm16: bytes
    recording_pcm16: bytes
    recording_sample_rate: int


def check_speech_to_text_readiness(
    server_url: str,
    *,
    post: Callable[..., httpx.Response] = httpx.post,
) -> str | None:
    """Return a stable activation error when server-side STT is not executable."""

    normalized_server_url = (server_url or "").rstrip("/")
    if not normalized_server_url:
        return _ERROR_NO_SERVER

    try:
        response = post(
            f"{normalized_server_url}/api/rpc",
            json={
                "method": _TASK_MODEL_STATUS_METHOD,
                "params": {"task_type": _TASK_SPEECH_TO_TEXT},
            },
            timeout=_RPC_TIMEOUT,
        )
    except httpx.RequestError:
        logger.warning("Speech-to-text readiness check could not reach the server", exc_info=True)
        return _ERROR_SERVER_UNREACHABLE

    if response.status_code != 200:
        logger.warning(
            "Speech-to-text readiness check failed: HTTP %s",
            response.status_code,
        )
        return _ERROR_SPEECH_TO_TEXT_READINESS_FAILED

    try:
        payload = response.json()
    except ValueError:
        logger.warning("Speech-to-text readiness check returned invalid JSON")
        return _ERROR_SPEECH_TO_TEXT_READINESS_FAILED
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        logger.warning("Speech-to-text readiness RPC was rejected")
        return _ERROR_SPEECH_TO_TEXT_READINESS_FAILED
    result = payload.get("result")
    if not isinstance(result, dict):
        logger.warning("Speech-to-text readiness RPC returned an invalid result")
        return _ERROR_SPEECH_TO_TEXT_READINESS_FAILED
    if result.get("configured") is False:
        return _ERROR_SPEECH_TO_TEXT_UNCONFIGURED
    if result.get("usable") is False:
        return _ERROR_SPEECH_TO_TEXT_UNAVAILABLE
    if result.get("configured") is not True or result.get("usable") is not True:
        logger.warning("Speech-to-text readiness RPC omitted readiness fields")
        return _ERROR_SPEECH_TO_TEXT_READINESS_FAILED
    return None


class ResamplingInputStream:
    """Read a native sounddevice stream as 16 kHz mono signed PCM frames."""

    def __init__(self, stream: Any, capture_format: CaptureFormat) -> None:
        self._stream = stream
        self.capture_format = capture_format

    def start(self) -> None:
        self._stream.start()

    def read_pcm16(self, target_frames: int) -> bytes:
        return self.read_capture_frame(target_frames).detection_pcm16

    def read_capture_frame(self, target_frames: int) -> CapturedAudioFrame:
        """Read native command audio plus its 16 kHz detection projection."""

        import numpy as np

        native_frames = max(
            1,
            round(target_frames * self.capture_format.sample_rate / _SAMPLE_RATE),
        )
        audio, _overflowed = self._stream.read(native_frames)
        samples = np.asarray(audio).reshape(-1)
        if self.capture_format.dtype == "float32":
            normalized = np.clip(samples.astype(np.float32), -1.0, 1.0)
        else:
            normalized = samples.astype(np.float32) / 32768.0

        native_pcm = np.clip(normalized * 32767.0, -32768, 32767).astype(np.int16)
        detection_samples = normalized
        if native_frames != target_frames:
            source_positions = np.linspace(0.0, 1.0, num=native_frames, endpoint=False)
            target_positions = np.linspace(0.0, 1.0, num=target_frames, endpoint=False)
            detection_samples = np.interp(target_positions, source_positions, normalized).astype(
                np.float32
            )

        detection_pcm = np.clip(detection_samples * 32767.0, -32768, 32767).astype(np.int16)
        return CapturedAudioFrame(
            detection_pcm16=bytes(detection_pcm.tobytes()),
            recording_pcm16=bytes(native_pcm.tobytes()),
            recording_sample_rate=self.capture_format.sample_rate,
        )

    def stop(self) -> None:
        self._stream.stop()

    def close(self) -> None:
        self._stream.close()


class WakewordWorker:
    """Orchestrates the wakeword detection → recording → transcription → send pipeline.

    The worker owns the microphone stream and runs the detection loop
    in a daemon thread. It publishes every state transition to the bridge so
    the WebUI can show live status.
    """

    def __init__(
        self,
        engine: Any,
        bridge: Any,
        settings_path: Path | None = None,
        server_url: str = "",
        config_reader: Callable[[], dict[str, Any]] | None = None,
        speech_readiness_checker: Callable[[str], str | None] | None = None,
        calibration_checker: Callable[[], bool] | None = None,
    ) -> None:
        self._engine = engine
        self._bridge = bridge
        self._settings_path = settings_path
        self._server_url = server_url.rstrip("/")
        self._config_reader = config_reader
        self._speech_readiness_checker = speech_readiness_checker or check_speech_to_text_readiness
        self._calibration_checker = calibration_checker or (lambda: False)
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._stream: Any = None

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Launch startup and detection work without blocking the Desktop bridge."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._running.set()
        self._bridge.publish_state("starting")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the detection loop to stop and release resources."""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._stop_engine()
        self._close_stream()

    def is_running(self) -> bool:
        """True while the detection thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    # -- Detection loop ------------------------------------------------------

    def _run(self) -> None:
        """Validate routing, start the engine, then detect and handle commands."""
        config = self._read_config()
        agent_id = config.get("target_agent_id")
        if not self._server_url:
            self._fail("no_server")
            return
        if not isinstance(agent_id, str) or not agent_id.strip():
            self._fail("missing_target_agent")
            return
        readiness_error = self._speech_readiness_checker(self._server_url)
        if readiness_error is not None:
            self._fail(readiness_error)
            return
        if not self._target_agent_available(agent_id):
            if not self._running.is_set():
                return
            self._fail("target_agent_unavailable")
            return
        if not self._running.is_set():
            return
        try:
            self._engine.start()
        except Exception:
            logger.warning("Failed to start wakeword engine", exc_info=True)
            self._fail("engine_start_failed")
            return
        if not self._running.is_set():
            self._stop_engine()
            return
        try:
            self._open_stream()
        except MicrophoneUnavailableError:
            logger.warning("No compatible microphone is available", exc_info=True)
            self._stop_engine()
            self._fail("microphone_unavailable")
            return
        except Exception:
            logger.warning("Failed to open microphone stream", exc_info=True)
            self._stop_engine()
            self._fail("microphone_unavailable")
            return

        self._bridge.publish_state("listening")
        consecutive_read_errors = 0
        detection_pre_roll: deque[CapturedAudioFrame] = deque(maxlen=_DETECTION_PRE_ROLL_CHUNKS)

        try:
            while self._running.is_set():
                try:
                    captured_frame = _read_capture_frame(self._stream, _FRAME_SIZE_SAMPLES)
                except Exception:
                    logger.warning("Microphone read error", exc_info=True)
                    consecutive_read_errors += 1
                    if consecutive_read_errors >= _MAX_CONSECUTIVE_MIC_READ_ERRORS:
                        self._fail("microphone_read_failed")
                        break
                    if self._restart_stream():
                        self._bridge.publish_state("listening")
                        continue
                    self._fail("microphone_read_failed")
                    break

                consecutive_read_errors = 0
                detection_pre_roll.append(captured_frame)
                calibrating = self._calibration_checker()

                try:
                    match = self._engine.detect(captured_frame.detection_pcm16)
                except Exception:
                    logger.warning("Wakeword detection failed", exc_info=True)
                    self._fail("detection_failed")
                    break
                if calibrating or self._calibration_checker():
                    detection_pre_roll.clear()
                    continue
                if match is not None:
                    logger.info(
                        "Wakeword detected: model=%s score=%.3f threshold=%.3f",
                        match.model_id,
                        match.score,
                        match.threshold,
                    )
                    self._bridge.publish_state("wakeword_detected")
                    outcome = self._handle_detection(tuple(detection_pre_roll))
                    detection_pre_roll.clear()
                    if not self._running.is_set():
                        break
                    self._prepare_next_listen(outcome)
                    if not self._running.is_set():
                        break
                    if not self._restart_stream():
                        self._fail("microphone_unavailable")
                        break
        finally:
            self._close_stream()
            self._stop_engine()

    def _stop_engine(self) -> None:
        """Release engine resources; implementations are expected to be idempotent."""
        try:
            self._engine.stop()
        except Exception:
            logger.warning("Error stopping wakeword engine", exc_info=True)

    def _open_stream(self) -> None:
        """Open a compatible native stream and normalize it to 16 kHz PCM."""
        import sounddevice as sd  # type: ignore[import-untyped]

        config = self._read_config()
        requested_device = config.get("microphone")
        device = requested_device if isinstance(requested_device, int) else None
        capture_format = _select_capture_format(sd, device)
        native_stream = sd.InputStream(
            samplerate=capture_format.sample_rate,
            channels=_CHANNELS,
            dtype=capture_format.dtype,
            blocksize=0,
            device=capture_format.device,
        )
        self._stream = ResamplingInputStream(native_stream, capture_format)
        self._stream.start()
        self._bridge.publish_runtime_details(
            active_microphone={
                "index": capture_format.device,
                "name": capture_format.name,
                "sample_rate": capture_format.sample_rate,
            }
        )

    def _close_stream(self) -> None:
        """Close the microphone stream."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                logger.warning("Error closing microphone stream", exc_info=True)
            self._stream = None

    def _restart_stream(self) -> bool:
        """Reset the microphone stream after overflow-prone pauses."""
        self._close_stream()
        try:
            self._open_stream()
        except Exception:
            logger.warning("Failed to reopen microphone stream", exc_info=True)
            return False
        return True

    def _prepare_next_listen(self, outcome: str | None) -> None:
        """Expose the completed outcome briefly, then return to listening."""
        self._close_stream()
        if outcome:
            self._bridge.publish_state(outcome)
            _sleep_while_running(self._running, _POST_DETECTION_LISTENING_HOLD_SECONDS)
            if not self._running.is_set():
                return
        self._bridge.publish_state("listening")

    # -- Post-detection pipeline ---------------------------------------------

    def _handle_detection(
        self,
        pre_roll_audio: bytes | tuple[CapturedAudioFrame, ...] = b"",
    ) -> str | None:
        """Record audio, transcribe, and send after wakeword detection."""
        config = self._read_config()
        agent_id = config.get("target_agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            logger.warning("Wakeword command ignored because no target agent is configured")
            self._fail("missing_target_agent")
            return None

        self._bridge.publish_state("recording")
        audio_data = self._record_until_silence(pre_roll_audio)
        if audio_data is None:
            return _OUTCOME_NO_SPEECH
        if not self._running.is_set():
            # Stopped (disabled/reconfigured) during recording — skip the network
            # round-trip entirely, no transcription and no send.
            return None

        self._bridge.publish_state("transcribing")
        self._close_stream()
        transcript = self._transcribe(audio_data)
        if transcript is None:
            logger.warning("Wakeword transcription failed; returning to listening")
            return _OUTCOME_TRANSCRIPTION_FAILED
        transcript = transcript.strip()
        if not transcript:
            logger.info("Wakeword recording produced no transcript; returning to listening")
            return _OUTCOME_NO_SPEECH
        if not self._running.is_set():
            # Stopped during transcription — do not send a now-stale command.
            logger.info("Wakeword worker stopped during transcription; discarding transcript")
            return None

        if _is_voice_cancel_phrase(transcript):
            logger.info("Wakeword command discarded by voice cancel phrase")
            return _OUTCOME_CANCELLED

        self._bridge.publish_state("sending")
        session_behavior = config.get("session_behavior", "active")

        session_id = self._resolve_session(agent_id, session_behavior)
        if not session_id:
            # A stop mid-resolve empties the result; that is not an error, so only
            # surface "error" when the worker is still meant to be running.
            if self._running.is_set():
                self._fail("session_resolution_failed")
            return None
        if not self._send_transcript(transcript, agent_id, session_id):
            if self._running.is_set():
                self._fail("send_failed")
            return None
        logger.info(
            "Wakeword command sent (agent_id=%s, session_behavior=%s)",
            agent_id,
            session_behavior,
        )
        return _OUTCOME_SENT

    def _read_config(self) -> dict[str, Any]:
        """Read the current wakeword configuration from Desktop settings."""
        if self._config_reader is not None:
            try:
                return self._config_reader()
            except Exception:
                logger.warning("Failed to read wakeword settings from bridge", exc_info=True)
                return {}
        try:
            from desktop.settings import read_wakeword_settings

            return read_wakeword_settings(self._settings_path)
        except Exception:
            logger.warning("Failed to read wakeword settings", exc_info=True)
            return {}

    def _target_agent_available(self, agent_id: str) -> bool:
        """Verify the server-specific target exists before opening the microphone."""
        return bool(self._rpc_call("agent.get", {"id": agent_id}))

    def _fail(self, error_code: str) -> None:
        """Stop the worker and expose one actionable stable failure reason."""
        logger.warning("Wakeword worker stopped (reason=%s)", error_code)
        self._running.clear()
        self._bridge.publish_state("error", error_code)

    # -- Audio recording -----------------------------------------------------

    def _record_until_silence(
        self,
        pre_roll_audio: bytes | tuple[CapturedAudioFrame, ...] = b"",
    ) -> bytes | None:
        """Capture microphone audio until silence or max duration.

        Uses webrtcvad for voice activity detection. Returns WAV-encoded
        audio bytes, or None when no frames were recorded.
        """
        import webrtcvad  # type: ignore[import-untyped]

        vad = webrtcvad.Vad(_VAD_MODE)
        frames: list[CapturedAudioFrame] = []
        pre_speech_frames: deque[CapturedAudioFrame] = deque(maxlen=_PRE_SPEECH_FRAME_COUNT)
        pre_speech_frames.extend(
            CapturedAudioFrame(frame, frame, _SAMPLE_RATE)
            for frame in _end_aligned_vad_frames(_detection_audio_bytes(pre_roll_audio))
        )
        silent_frames = 0
        has_speech = False
        waited_frames = 0

        while self._running.is_set():
            try:
                frame = _read_capture_frame(self._stream, _VAD_FRAME_SIZE)
            except Exception:
                logger.warning("Microphone read error during recording", exc_info=True)
                break

            try:
                is_speech = vad.is_speech(frame.detection_pcm16, _SAMPLE_RATE)
            except Exception:
                is_speech = True

            if is_speech:
                if not has_speech:
                    frames.extend(pre_speech_frames)
                has_speech = True
                frames.append(frame)
                silent_frames = 0
            elif has_speech:
                frames.append(frame)
                silent_frames += 1
            else:
                pre_speech_frames.append(frame)
                waited_frames += 1

            if has_speech and silent_frames >= _SILENCE_FRAME_COUNT:
                break
            if len(frames) >= _MAX_RECORDING_FRAMES:
                break
            if not has_speech and waited_frames >= _SPEECH_START_FRAME_COUNT:
                break

        if not frames or not has_speech:
            return None

        return _encode_captured_audio(frames)

    # -- Server communication ------------------------------------------------

    def _transcribe(self, audio_data: bytes) -> str | None:
        """Upload recorded audio to the server speech endpoint."""
        if not self._server_url:
            return None

        url = f"{self._server_url}/api/speech/transcribe"
        files = {"file": ("recording.wav", audio_data, "audio/wav")}

        for attempt in range(_MAX_RETRIES):
            try:
                response = httpx.post(url, files=files, timeout=_HTTP_TIMEOUT)
                if response.status_code == 200:
                    result = response.json()
                    if not isinstance(result, dict):
                        return None
                    transcript = result.get("text") or result.get("transcript", "")
                    if isinstance(transcript, str):
                        return transcript
                    return None
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    if not self._running.is_set():
                        return None
                    _backoff_sleep(attempt, self._running)
                    continue
                logger.warning(
                    "Speech transcription failed: HTTP %s %s",
                    response.status_code,
                    _response_text_preview(response),
                )
                return None
            except httpx.RequestError:
                if attempt < _MAX_RETRIES - 1 and self._running.is_set():
                    _backoff_sleep(attempt, self._running)
                    continue
                logger.warning("Speech transcription request failed", exc_info=True)
                return None
        return None

    def _resolve_session(self, agent_id: str, behavior: str) -> str:
        """Resolve or create a session for the given agent."""
        if behavior == "new":
            return self._create_session(agent_id)
        current_session_id = self._current_session_id(agent_id)
        if current_session_id:
            return current_session_id
        # Fallback for agents without current_session_id: pick most recently active, or create one.
        sessions = self._list_sessions(agent_id)
        if sessions:
            latest = max(sessions, key=lambda session: str(session.get("last_active_at", "")))
            session_id = latest.get("id", "")
            return session_id if isinstance(session_id, str) else ""
        return self._create_session(agent_id)

    def _current_session_id(self, agent_id: str) -> str:
        """Return the agent's persisted current session id, if available."""
        result = self._rpc_call("agent.get", {"id": agent_id})
        session_id = result.get("current_session_id", "")
        return session_id if isinstance(session_id, str) else ""

    def _list_sessions(self, agent_id: str) -> list[dict[str, Any]]:
        """List sessions for an agent via the session.list RPC."""
        sessions = self._rpc_call("session.list", {"agent_id": agent_id}).get("sessions", [])
        return sessions if isinstance(sessions, list) else []

    def _create_session(self, agent_id: str) -> str:
        """Create a new session for an agent and return its ID."""
        result = self._rpc_call("session.create", {"agent_id": agent_id, "make_current": True})
        session_id = result.get("session_id") or result.get("id", "")
        return session_id if isinstance(session_id, str) else ""

    def _send_transcript(self, transcript: str, agent_id: str, session_id: str) -> bool:
        """Send the transcribed text as a chat message via RPC."""
        result = self._rpc_call(
            "chat.stream",
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "content": transcript,
                "input_origin": "speech_transcription",
            },
        )
        return bool(result)

    def _rpc_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Make a JSON-RPC call to the vBot server. Returns result dict or {}."""
        if not self._server_url:
            return {}

        url = f"{self._server_url}/api/rpc"
        payload = {"method": method, "params": params}

        for attempt in range(_MAX_RETRIES):
            try:
                response = httpx.post(url, json=payload, timeout=_RPC_TIMEOUT)
                if response.status_code == 200:
                    rpc_response = response.json()
                    if not isinstance(rpc_response, dict):
                        logger.warning("RPC %s returned a non-object response", method)
                        return {}
                    if rpc_response.get("ok") is False:
                        error = rpc_response.get("error", {})
                        message = (
                            error.get("message", "unknown RPC error")
                            if isinstance(error, dict)
                            else "unknown RPC error"
                        )
                        logger.warning("RPC %s failed: %s", method, message)
                        return {}
                    result = rpc_response.get("result", {})
                    return result if isinstance(result, dict) else {}
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    if not self._running.is_set():
                        return {}
                    _backoff_sleep(attempt, self._running)
                    continue
                logger.warning("RPC %s failed: HTTP %s", method, response.status_code)
                return {}
            except httpx.RequestError:
                if attempt < _MAX_RETRIES - 1 and self._running.is_set():
                    _backoff_sleep(attempt, self._running)
                    continue
                logger.warning("RPC %s request failed", method, exc_info=True)
                return {}
        return {}


# -- Helpers ----------------------------------------------------------------


def _read_capture_frame(stream: Any, target_frames: int) -> CapturedAudioFrame:
    reader = getattr(stream, "read_capture_frame", None)
    if callable(reader):
        frame = reader(target_frames)
        if isinstance(frame, CapturedAudioFrame):
            return frame
        raise TypeError("Capture stream returned an invalid audio frame")
    detection_pcm = stream.read_pcm16(target_frames)
    return CapturedAudioFrame(
        detection_pcm16=detection_pcm,
        recording_pcm16=detection_pcm,
        recording_sample_rate=_SAMPLE_RATE,
    )


def _detection_audio_bytes(
    audio: bytes | tuple[CapturedAudioFrame, ...],
) -> bytes:
    if isinstance(audio, bytes):
        return audio
    return b"".join(frame.detection_pcm16 for frame in audio)


def _encode_captured_audio(frames: list[CapturedAudioFrame]) -> bytes:
    recording_sample_rate = frames[-1].recording_sample_rate
    raw_frames = b"".join(
        _resample_pcm16(
            frame.recording_pcm16,
            frame.recording_sample_rate,
            recording_sample_rate,
        )
        for frame in frames
    )
    return _encode_wav(raw_frames, sample_rate=recording_sample_rate)


def _resample_pcm16(audio: bytes, source_rate: int, target_rate: int) -> bytes:
    if not audio or source_rate == target_rate:
        return audio

    import numpy as np

    samples = np.frombuffer(audio, dtype=np.int16)
    target_count = max(1, round(len(samples) * target_rate / source_rate))
    if len(samples) == 1:
        converted = np.repeat(samples, target_count)
    else:
        source_positions = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
        target_positions = np.linspace(0.0, 1.0, num=target_count, endpoint=False)
        converted = np.interp(target_positions, source_positions, samples).astype(np.int16)
    return bytes(converted.tobytes())


def _encode_wav(raw_frames: bytes, *, sample_rate: int = _SAMPLE_RATE) -> bytes:
    """Wrap mono 16-bit PCM frames in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(_CHANNELS)
        wav_file.setsampwidth(_SAMPLE_WIDTH)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(raw_frames)
    return buffer.getvalue()


def _end_aligned_vad_frames(audio: bytes) -> list[bytes]:
    """Split PCM into full VAD frames while retaining the newest audio."""
    frame_bytes = _VAD_FRAME_SIZE * _SAMPLE_WIDTH
    if len(audio) < frame_bytes:
        return []
    complete_audio = audio[len(audio) % frame_bytes :]
    return [
        complete_audio[offset : offset + frame_bytes]
        for offset in range(0, len(complete_audio), frame_bytes)
    ]


def _backoff_sleep(attempt: int, running: threading.Event | None = None) -> None:
    """Sleep with exponential backoff and jitter, interruptible by ``running``.

    With a running flag, the sleep ends early when the worker is stopped
    mid-backoff, so a disable does not wait out the full delay before the retry
    loop notices it should bail.
    """
    delay = min((2**attempt) + random.random(), 10.0)
    if running is None:
        time.sleep(delay)
        return
    _sleep_while_running(running, delay)


def _sleep_while_running(running: threading.Event, duration_seconds: float) -> None:
    """Sleep in small slices so stop() can interrupt the post-detection hold."""
    deadline = time.monotonic() + max(0.0, duration_seconds)
    while running.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(_INTERRUPTIBLE_SLEEP_SLICE_SECONDS, remaining))


def _response_text_preview(response: httpx.Response) -> str:
    """Return a bounded response-body preview for diagnostics."""
    try:
        text = response.text.strip()
    except Exception:
        return ""
    if not text:
        return ""
    return text[:500]


def _is_voice_cancel_phrase(transcript: str) -> bool:
    """Return whether a normalized transcript ends with a reserved cancel phrase."""
    normalized = re.sub(r"[^\wäöüß]+", " ", transcript.casefold(), flags=re.UNICODE).strip()
    return any(
        normalized == phrase or normalized.endswith(f" {phrase}")
        for phrase in _VOICE_CANCEL_PHRASES
    )


def _candidate_device_indices(sd: Any, requested_device: int | None) -> list[int]:
    """Return requested/default/fallback input devices in safe preference order."""
    devices = sd.query_devices()
    if requested_device is not None:
        return [requested_device]

    candidates: list[int] = []
    try:
        default_input = int(sd.default.device[0])
    except (IndexError, TypeError, ValueError):
        default_input = -1
    if default_input >= 0:
        candidates.append(default_input)
    for host_api in sd.query_hostapis():
        host_default = host_api.get("default_input_device", -1)
        if isinstance(host_default, int) and host_default >= 0:
            candidates.append(host_default)
    candidates.extend(
        index for index, info in enumerate(devices) if int(info.get("max_input_channels", 0)) > 0
    )
    return list(dict.fromkeys(candidates))


def _capture_format_for_device(sd: Any, device: int) -> CaptureFormat | None:
    """Find the best native format that can be normalized to 16 kHz mono PCM."""
    try:
        info = sd.query_devices(device)
    except Exception:
        return None
    if int(info.get("max_input_channels", 0)) <= 0:
        return None

    default_rate = int(info.get("default_samplerate", 0) or 0)
    sample_rates = list(dict.fromkeys([_SAMPLE_RATE, default_rate, *_COMMON_CAPTURE_SAMPLE_RATES]))
    for sample_rate in sample_rates:
        if sample_rate < _SAMPLE_RATE:
            continue
        for dtype in _CAPTURE_DTYPES:
            try:
                sd.check_input_settings(
                    device=device,
                    samplerate=sample_rate,
                    channels=_CHANNELS,
                    dtype=dtype,
                )
            except Exception:
                continue
            return CaptureFormat(
                device=device,
                name=str(info.get("name", f"Device {device}")),
                sample_rate=sample_rate,
                dtype=dtype,
            )
    return None


def _select_capture_format(sd: Any, requested_device: int | None) -> CaptureFormat:
    """Select a usable requested or automatic input format."""
    for device in _candidate_device_indices(sd, requested_device):
        capture_format = _capture_format_for_device(sd, device)
        if capture_format is not None:
            return capture_format
    raise MicrophoneUnavailableError("No input device supports Voice capture")


def list_microphones() -> list[dict[str, Any]]:
    """Enumerate input devices and surface Voice-format compatibility."""
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
    except ImportError:
        return []

    devices: list[dict[str, Any]] = []
    try:
        for i, info in enumerate(sd.query_devices()):
            if int(info.get("max_input_channels", 0)) > 0:
                capture_format = _capture_format_for_device(sd, i)
                devices.append(
                    {
                        "index": i,
                        "name": info.get("name", f"Device {i}"),
                        "default_sample_rate": int(info.get("default_samplerate", _SAMPLE_RATE)),
                        "supported": capture_format is not None,
                        "capture_sample_rate": (
                            capture_format.sample_rate if capture_format is not None else None
                        ),
                    }
                )
    except Exception:
        logger.warning("Failed to enumerate microphones", exc_info=True)
    return devices


class UnavailableWakewordWorker:
    """Stable non-simulating worker used when the local Voice stack is absent."""

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    def start(self) -> None:
        self._bridge.publish_state("error", "voice_stack_unavailable")

    def stop(self) -> None:
        return

    def is_running(self) -> bool:
        return False


class MockWakewordWorker:
    """No-microphone worker used when the real wakeword stack is unavailable.

    Drives the *same* detection → recording → transcribing → sending state cycle
    the real worker publishes, but from a :class:`MockWakewordEngine` score script
    instead of a live microphone, and with no network calls. This lets the WebUI
    status indicator be validated with ``--mock-wakeword`` (and makes the mock
    fallback visibly "alive" rather than frozen on ``listening``).
    """

    def __init__(self, bridge: Any, engine: Any = None) -> None:
        self._bridge = bridge
        self._engine = engine if engine is not None else MockWakewordEngine(_MOCK_DEFAULT_SCORES)
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    def start(self) -> None:
        """Start the simulated state loop without opening audio devices."""
        if self.is_running():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the simulated state loop."""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def is_running(self) -> bool:
        """True while the mock loop thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        try:
            self._engine.start()
        except Exception:
            logger.warning("Mock wakeword engine failed to start", exc_info=True)
        self._bridge.publish_state("listening")
        while self._running.is_set():
            _sleep_while_running(self._running, _MOCK_FRAME_SECONDS)
            if not self._running.is_set():
                break
            match = self._engine.detect(b"")
            if match is not None:
                self._simulate_cycle()
                if self._running.is_set():
                    self._bridge.publish_state("listening")

    def _simulate_cycle(self) -> None:
        """Publish one full post-detection state sequence with brief dwells."""
        for state in ("wakeword_detected", "recording", "transcribing", "sending", "sent"):
            if not self._running.is_set():
                return
            self._bridge.publish_state(state)
            _sleep_while_running(self._running, _MOCK_STAGE_SECONDS)
