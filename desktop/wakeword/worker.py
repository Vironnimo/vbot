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
            self._bridge.publish_state("error")
            return

        self._bridge.publish_state("listening")

        try:
            while self._running.is_set():
                try:
                    chunk = self._stream.read(_FRAME_SIZE_SAMPLES)[0].tobytes()
                except Exception:
                    logger.warning("Microphone read error", exc_info=True)
                    self._bridge.publish_state("error")
                    break

                score = self._engine.detect(chunk)
                if score >= getattr(self._engine, "threshold", 0.5):
                    self._bridge.publish_state("wakeword_detected")
                    self._handle_detection()
                    if not self._running.is_set():
                        break
                    self._bridge.publish_state("listening")
        finally:
            self._close_stream()

    def _open_stream(self) -> None:
        """Open the microphone stream at 16kHz mono 16-bit via sounddevice."""
        import sounddevice as sd  # type: ignore[import-untyped]

        self._stream = sd.InputStream(
            samplerate=_SAMPLE_RATE,
            channels=_CHANNELS,
            dtype="int16",
            blocksize=_FRAME_SIZE_SAMPLES,
        )
        self._stream.start()

    def _close_stream(self) -> None:
        """Close the microphone stream."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # -- Post-detection pipeline ---------------------------------------------

    def _handle_detection(self) -> None:
        """Record audio, transcribe, and send after wakeword detection."""
        config = self._read_config()

        self._bridge.publish_state("recording")
        audio_data = self._record_until_silence()
        if audio_data is None:
            if self._running.is_set():
                self._bridge.publish_state("listening")
            return

        self._bridge.publish_state("transcribing")
        transcript = self._transcribe(audio_data)
        if not transcript:
            self._bridge.publish_state("error")
            return

        self._bridge.publish_state("sending")
        agent_id = config.get("target_agent_id")
        session_behavior = config.get("session_behavior", "active")

        if agent_id:
            session_id = self._resolve_session(agent_id, session_behavior)
            if session_id:
                self._send_transcript(transcript, agent_id, session_id)

        if self._running.is_set():
            self._bridge.publish_state("listening")

    def _read_config(self) -> dict[str, Any]:
        """Read the current wakeword configuration from Desktop settings."""
        try:
            from desktop.main import read_wakeword_settings

            return read_wakeword_settings(self._settings_path)
        except Exception:
            return {}

    # -- Audio recording -----------------------------------------------------

    def _record_until_silence(self) -> bytes | None:
        """Capture microphone audio until silence or max duration.

        Uses webrtcvad for voice activity detection. Returns WAV-encoded
        audio bytes, or None when no frames were recorded.
        """
        try:
            import webrtcvad
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
        files = {"audio": ("recording.wav", audio_data, "audio/wav")}

        for attempt in range(_MAX_RETRIES):
            try:
                response = httpx.post(url, files=files, timeout=_HTTP_TIMEOUT)
                if response.status_code == 200:
                    result = response.json()
                    return result.get("text") or result.get("transcript", "")
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    _backoff_sleep(attempt)
                    continue
                logger.warning("Speech transcription failed: HTTP %s", response.status_code)
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
        # "active" behavior: pick the latest session, or create one
        sessions = self._list_sessions(agent_id)
        if sessions:
            return sessions[0].get("id", "")
        return self._create_session(agent_id)

    def _list_sessions(self, agent_id: str) -> list[dict[str, Any]]:
        """List sessions for an agent via the session.list RPC."""
        return self._rpc_call("session.list", {"agent_id": agent_id}).get("sessions", [])

    def _create_session(self, agent_id: str) -> str:
        """Create a new session for an agent and return its ID."""
        result = self._rpc_call("session.create", {"agent_id": agent_id, "make_current": True})
        return result.get("session_id") or result.get("id", "")

    def _send_transcript(self, transcript: str, agent_id: str, session_id: str) -> None:
        """Send the transcribed text as a chat message via RPC."""
        self._rpc_call(
            "chat.send",
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "content": transcript,
            },
        )

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
                    return response.json().get("result", {})
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
        pass
    return devices
