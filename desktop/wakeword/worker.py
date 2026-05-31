"""Wakeword worker thread — detection → recording → transcription → sending.

Runs in a daemon thread and publishes state transitions through the bridge
so the WebUI can show live status via poll-based `getWakewordStatus()`.
"""

from __future__ import annotations

import io
import logging
import random
import threading
import time
import wave
from pathlib import Path
from typing import Any

import httpx

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

_MAX_RECORDING_SECONDS = 15.0
_MAX_RECORDING_FRAMES = int(_MAX_RECORDING_SECONDS / (_VAD_FRAME_DURATION_MS / 1000))
_MAX_CONSECUTIVE_MIC_READ_ERRORS = 3
_POST_DETECTION_LISTENING_HOLD_SECONDS = 1.0
_INTERRUPTIBLE_SLEEP_SLICE_SECONDS = 0.05

_HTTP_TIMEOUT = 30.0
_RPC_TIMEOUT = 10.0
_MAX_RETRIES = 3

_RETRYABLE_STATUS_CODES = frozenset([429, 502, 503])


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
    ) -> None:
        self._engine = engine
        self._bridge = bridge
        self._settings_path = settings_path
        self._server_url = server_url.rstrip("/")
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._stream: Any = None

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the engine and launch the detection thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._running.set()
        try:
            self._engine.start()
        except Exception:
            logger.warning("Failed to start wakeword engine", exc_info=True)
            self._running.clear()
            self._bridge.publish_state("error")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the detection loop to stop and release resources."""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        try:
            self._engine.stop()
        except Exception:
            logger.warning("Error stopping wakeword engine", exc_info=True)
        self._close_stream()

    def is_running(self) -> bool:
        """True while the detection thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    # -- Detection loop ------------------------------------------------------

    def _run(self) -> None:
        """Main detection loop: open mic, listen, detect, handle, repeat."""
        try:
            self._open_stream()
        except Exception:
            logger.warning("Failed to open microphone stream", exc_info=True)
            self._running.clear()
            self._bridge.publish_state("error")
            return

        self._bridge.publish_state("listening")
        consecutive_read_errors = 0
        wakeword_armed = True

        try:
            while self._running.is_set():
                try:
                    chunk = self._stream.read(_FRAME_SIZE_SAMPLES)[0].tobytes()
                except Exception:
                    logger.warning("Microphone read error", exc_info=True)
                    consecutive_read_errors += 1
                    if consecutive_read_errors >= _MAX_CONSECUTIVE_MIC_READ_ERRORS:
                        self._running.clear()
                        self._bridge.publish_state("error")
                        break
                    if self._restart_stream():
                        self._bridge.publish_state("listening")
                        continue
                    self._running.clear()
                    self._bridge.publish_state("error")
                    break

                consecutive_read_errors = 0

                try:
                    score = self._engine.detect(chunk)
                except Exception:
                    logger.warning("Wakeword detection failed", exc_info=True)
                    self._running.clear()
                    self._bridge.publish_state("error")
                    break
                threshold = getattr(self._engine, "threshold", 0.5)
                if not wakeword_armed:
                    if score < threshold:
                        wakeword_armed = True
                    continue
                if score >= threshold:
                    wakeword_armed = False
                    self._bridge.publish_state("wakeword_detected")
                    self._handle_detection()
                    if not self._running.is_set():
                        break
                    self._prepare_next_listen()
                    if not self._running.is_set():
                        break
                    if not self._restart_stream():
                        self._running.clear()
                        self._bridge.publish_state("error")
                        break
        finally:
            self._close_stream()

    def _open_stream(self) -> None:
        """Open the microphone stream at 16kHz mono 16-bit via sounddevice."""
        import sounddevice as sd  # type: ignore[import-untyped]

        config = self._read_config()
        device = config.get("microphone")
        stream_options: dict[str, Any] = {
            "samplerate": _SAMPLE_RATE,
            "channels": _CHANNELS,
            "dtype": "int16",
            "blocksize": _FRAME_SIZE_SAMPLES,
        }
        if isinstance(device, int):
            stream_options["device"] = device
        self._stream = sd.InputStream(
            **stream_options,
        )
        self._stream.start()

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

    def _prepare_next_listen(self) -> None:
        """Return to a visible listening state before re-arming wakeword detection."""
        self._close_stream()
        self._bridge.publish_state("listening")
        _sleep_while_running(self._running, _POST_DETECTION_LISTENING_HOLD_SECONDS)

    # -- Post-detection pipeline ---------------------------------------------

    def _handle_detection(self) -> None:
        """Record audio, transcribe, and send after wakeword detection."""
        config = self._read_config()
        agent_id = config.get("target_agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            logger.warning("Wakeword command ignored because no target agent is configured")
            self._bridge.publish_state("error")
            self._running.clear()
            return

        self._bridge.publish_state("recording")
        audio_data = self._record_until_silence()
        if audio_data is None:
            return

        self._bridge.publish_state("transcribing")
        self._close_stream()
        transcript = self._transcribe(audio_data)
        if transcript is None:
            logger.warning("Wakeword transcription failed; returning to listening")
            return
        transcript = transcript.strip()
        if not transcript:
            logger.info("Wakeword recording produced no transcript; returning to listening")
            return

        self._bridge.publish_state("sending")
        session_behavior = config.get("session_behavior", "active")

        session_id = self._resolve_session(agent_id, session_behavior)
        if not session_id:
            self._bridge.publish_state("error")
            self._running.clear()
            return
        if not self._send_transcript(transcript, agent_id, session_id):
            self._bridge.publish_state("error")
            self._running.clear()
            return

    def _read_config(self) -> dict[str, Any]:
        """Read the current wakeword configuration from Desktop settings."""
        try:
            from desktop.main import read_wakeword_settings

            return read_wakeword_settings(self._settings_path)
        except Exception:
            logger.warning("Failed to read wakeword settings", exc_info=True)
            return {}

    # -- Audio recording -----------------------------------------------------

    def _record_until_silence(self) -> bytes | None:
        """Capture microphone audio until silence or max duration.

        Uses webrtcvad for voice activity detection. Returns WAV-encoded
        audio bytes, or None when no frames were recorded.
        """
        try:
            import webrtcvad  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("webrtcvad not available; recording raw audio")
            return self._record_raw()

        vad = webrtcvad.Vad(_VAD_MODE)
        frames: list[bytes] = []
        silent_frames = 0
        has_speech = False

        while self._running.is_set():
            try:
                frame = self._stream.read(_VAD_FRAME_SIZE)[0].tobytes()
            except Exception:
                logger.warning("Microphone read error during recording", exc_info=True)
                break

            frames.append(frame)

            try:
                is_speech = vad.is_speech(frame, _SAMPLE_RATE)
            except Exception:
                is_speech = True

            if is_speech:
                has_speech = True
                silent_frames = 0
            else:
                silent_frames += 1

            if silent_frames >= _SILENCE_FRAME_COUNT:
                break
            if len(frames) >= _MAX_RECORDING_FRAMES:
                break

        if not frames or not has_speech:
            return None

        return _encode_wav(b"".join(frames))

    def _record_raw(self) -> bytes | None:
        """Fallback recording without VAD — fixed 3-second capture."""
        frames: list[bytes] = []
        max_frames = int(3.0 / (_VAD_FRAME_DURATION_MS / 1000))
        for _ in range(max_frames):
            if not self._running.is_set():
                break
            try:
                frame = self._stream.read(_VAD_FRAME_SIZE)[0].tobytes()
                frames.append(frame)
            except Exception:
                logger.warning("Microphone read error during raw recording", exc_info=True)
                break
        if not frames:
            return None
        return _encode_wav(b"".join(frames))

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
                    _backoff_sleep(attempt)
                    continue
                logger.warning(
                    "Speech transcription failed: HTTP %s %s",
                    response.status_code,
                    _response_text_preview(response),
                )
                return None
            except httpx.RequestError:
                if attempt < _MAX_RETRIES - 1:
                    _backoff_sleep(attempt)
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
                    _backoff_sleep(attempt)
                    continue
                logger.warning("RPC %s failed: HTTP %s", method, response.status_code)
                return {}
            except httpx.RequestError:
                if attempt < _MAX_RETRIES - 1:
                    _backoff_sleep(attempt)
                    continue
                logger.warning("RPC %s request failed", method, exc_info=True)
                return {}
        return {}


# -- Helpers ----------------------------------------------------------------


def _encode_wav(raw_frames: bytes) -> bytes:
    """Wrap raw 16kHz mono 16-bit PCM frames in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(_CHANNELS)
        wav_file.setsampwidth(_SAMPLE_WIDTH)
        wav_file.setframerate(_SAMPLE_RATE)
        wav_file.writeframes(raw_frames)
    return buffer.getvalue()


def _backoff_sleep(attempt: int) -> None:
    """Sleep with exponential backoff and jitter."""
    delay = (2**attempt) + random.random()
    time.sleep(min(delay, 10.0))


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


def list_microphones() -> list[dict[str, Any]]:
    """Enumerate available input audio devices via sounddevice."""
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
    except ImportError:
        return []

    devices: list[dict[str, Any]] = []
    try:
        for i, info in enumerate(sd.query_devices()):
            if int(info.get("max_input_channels", 0)) > 0:
                devices.append(
                    {
                        "index": i,
                        "name": info.get("name", f"Device {i}"),
                        "default_sample_rate": int(info.get("default_samplerate", _SAMPLE_RATE)),
                    }
                )
    except Exception:
        logger.warning("Failed to enumerate microphones", exc_info=True)
    return devices


class MockWakewordWorker:
    """No-microphone worker used when real wakeword dependencies are unavailable."""

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    def start(self) -> None:
        """Start a lightweight status loop without opening audio devices."""
        if self.is_running():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the mock status loop."""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def is_running(self) -> bool:
        """True while the mock status thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        self._bridge.publish_state("listening")
        while self._running.is_set():
            time.sleep(0.1)
