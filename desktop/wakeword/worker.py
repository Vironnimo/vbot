"""Wakeword worker thread — detection → recording → transcription → sending.

Runs in a daemon thread and publishes state transitions through the bridge
so the WebUI can show live status via poll-based `getWakewordStatus()`."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx

from desktop.wakeword._audio_capture import (
    CapturedAudioFrame,
    CaptureFormat,
    MicrophoneCaptureError,
    MicrophoneUnavailableError,
    ResamplingInputStream,
    _detection_audio_bytes,
    _encode_captured_audio,
    _end_aligned_vad_frames,
    _read_capture_frame,
)
from desktop.wakeword._microphones import (
    _select_capture_format,
    list_microphones,
    refresh_microphone_devices,
)
from desktop.wakeword._speech_detection import (
    SpeechDetector,
    _chunk_contains_speech,
    _create_detection_vad,
    _create_recording_fallback_vad,
    _frame_is_speech,
)
from desktop.wakeword._worker_constants import (
    _AUDIO_BACKEND_LOCK,
    _CHANNELS,
    _DETECTION_PRE_ROLL_CHUNKS,
    _FRAME_SIZE_SAMPLES,
    _HTTP_TIMEOUT,
    _MAX_CONSECUTIVE_MIC_READ_ERRORS,
    _MAX_RETRIES,
    _MICROPHONE_RECONNECT_INTERVAL_SECONDS,
    _NO_SPEECH_DETECTOR_YET,
    _OUTCOME_CANCELLED,
    _OUTCOME_NO_SPEECH,
    _OUTCOME_SENT,
    _OUTCOME_TRANSCRIPTION_FAILED,
    _POST_DETECTION_LISTENING_HOLD_SECONDS,
    _PRE_SPEECH_FRAME_COUNT,
    _RETRYABLE_RPC_METHODS,
    _RETRYABLE_STATUS_CODES,
    _RPC_TIMEOUT,
    _SAMPLE_RATE,
    _SILENCE_FRAME_COUNT,
    _SPEECH_START_FRAME_COUNT,
    _SPEECH_UPLOAD_LIMIT_SAFETY_MARGIN_FRACTION,
    _UPLOAD_BUDGET_FALLBACK_BYTES,
    _UPLOAD_BUDGET_SETTING_PATH,
    _VAD_FRAME_SIZE,
    _WAV_HEADER_BYTES,
    logger,
)
from desktop.wakeword._worker_modes import (
    MockWakewordWorker,
    UnavailableWakewordWorker,
)
from desktop.wakeword._worker_support import (
    _backoff_sleep,
    _is_voice_cancel_phrase,
    _response_text_preview,
    _sleep_while_running,
    check_speech_to_text_readiness,
)

__all__ = [
    "CaptureFormat",
    "CapturedAudioFrame",
    "MicrophoneCaptureError",
    "MicrophoneUnavailableError",
    "MockWakewordWorker",
    "ResamplingInputStream",
    "SpeechDetector",
    "UnavailableWakewordWorker",
    "WakewordWorker",
    "check_speech_to_text_readiness",
    "list_microphones",
    "logger",
    "refresh_microphone_devices",
]


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
        speech_detector: SpeechDetector | None | object = _NO_SPEECH_DETECTOR_YET,
    ) -> None:
        self._engine = engine
        self._bridge = bridge
        self._settings_path = settings_path
        self._server_url = server_url.rstrip("/")
        self._config_reader = config_reader
        self._speech_readiness_checker = speech_readiness_checker or check_speech_to_text_readiness
        self._calibration_checker = calibration_checker or (lambda: False)
        # The neural speech detector is created lazily on first use so a missing
        # optional onnxruntime dependency only degrades endpointing quality, and
        # tests can inject a scripted detector (or None to force the fallback).
        self._speech_detector_override = speech_detector
        self._speech_detector: SpeechDetector | None | object = _NO_SPEECH_DETECTOR_YET
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._running = threading.Event()
        self._stop_recording = threading.Event()
        self._state_publish_lock = threading.Lock()
        self._stream: Any = None
        self._upload_budget_pcm16_bytes: int | None = None

    def _resolve_upload_budget_bytes(self) -> int:
        """Resolve the speech upload ceiling as a PCM16 payload budget.

        Asks the server for its active `speech.upload_max_size_bytes` once per
        worker and keeps a conservative fraction as WAV payload budget so the
        recording never exceeds what /api/speech/transcribe would accept. A
        failed read falls back to the mirrored default limit, never to "no
        budget".
        """
        if self._upload_budget_pcm16_bytes is None:
            self._upload_budget_pcm16_bytes = max(
                _WAV_HEADER_BYTES + 2,  # always room for at least one sample
                self._fetch_speech_upload_limit_bytes(),
            )
        return self._upload_budget_pcm16_bytes

    def _fetch_speech_upload_limit_bytes(self) -> int:
        """Query the server's active speech upload limit in payload bytes."""
        result = self._rpc_call("settings.get_path", {"path": _UPLOAD_BUDGET_SETTING_PATH})
        raw_value = result.get("setting", {}).get("value")
        if isinstance(raw_value, (int, float)) and raw_value > _WAV_HEADER_BYTES:
            payload = int(raw_value - _WAV_HEADER_BYTES)
            budget = int(payload * _SPEECH_UPLOAD_LIMIT_SAFETY_MARGIN_FRACTION)
            return max(budget, _WAV_HEADER_BYTES + 2)
        logger.warning("Speech upload limit unavailable from server; using default budget")
        return int(_UPLOAD_BUDGET_FALLBACK_BYTES * _SPEECH_UPLOAD_LIMIT_SAFETY_MARGIN_FRACTION)

    def _get_speech_detector(self) -> SpeechDetector | None:
        """Resolve the neural speech detector once, caching the fail-open result."""
        if self._speech_detector is _NO_SPEECH_DETECTOR_YET:
            if self._speech_detector_override is _NO_SPEECH_DETECTOR_YET:
                self._speech_detector = SpeechDetector.create()
            else:
                self._speech_detector = self._speech_detector_override
        # After resolution the value is a detector instance, fail-open None, or
        # a test-injected scripted equivalent, never the sentinel again.
        return cast("SpeechDetector | None", self._speech_detector)

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Launch startup and detection work without blocking the Desktop bridge."""
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            with self._state_publish_lock:
                self._running.set()
                self._bridge.publish_state("starting")
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Signal the detection loop to stop and release resources."""
        with self._state_publish_lock:
            self._running.clear()
        with self._thread_lock:
            thread = self._thread
            if thread is not None:
                thread.join(timeout=3.0)
                if not thread.is_alive() and self._thread is thread:
                    self._thread = None
        self._stop_engine()
        self._close_stream()

    def is_running(self) -> bool:
        """True while the detection thread is alive."""
        with self._thread_lock:
            return self._thread is not None and self._thread.is_alive()

    # -- Detection loop ------------------------------------------------------

    def _run(self) -> None:
        """Keep every unexpected pipeline failure visible and fully cleaned up."""
        try:
            self._run_pipeline()
        except Exception:
            logger.exception("Wakeword pipeline stopped unexpectedly")
            self._fail("pipeline_failed")
            self._close_stream()
            self._stop_engine()

    def _run_pipeline(self) -> None:
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
            if not self._recover_microphone("microphone_unavailable"):
                self._stop_engine()
                return
        except Exception:
            logger.warning("Failed to open microphone stream", exc_info=True)
            if not self._recover_microphone("microphone_unavailable"):
                self._stop_engine()
                return
        else:
            if not self._publish_state_if_running("listening"):
                self._close_stream()
                self._stop_engine()
                return
        consecutive_read_errors = 0
        detection_pre_roll: deque[CapturedAudioFrame] = deque(maxlen=_DETECTION_PRE_ROLL_CHUNKS)
        detection_vad = _create_detection_vad()
        speech_detector = self._get_speech_detector()

        try:
            while self._running.is_set():
                try:
                    captured_frame = _read_capture_frame(self._stream, _FRAME_SIZE_SAMPLES)
                except Exception:
                    logger.warning("Microphone read error", exc_info=True)
                    consecutive_read_errors += 1
                    if consecutive_read_errors >= _MAX_CONSECUTIVE_MIC_READ_ERRORS:
                        if not self._recover_microphone("microphone_read_failed"):
                            break
                        consecutive_read_errors = 0
                        detection_pre_roll.clear()
                        continue
                    if self._restart_stream():
                        if not self._publish_state_if_running("listening"):
                            break
                        continue
                    if not self._recover_microphone("microphone_read_failed"):
                        break
                    consecutive_read_errors = 0
                    detection_pre_roll.clear()
                    continue

                consecutive_read_errors = 0
                detection_pre_roll.append(captured_frame)
                calibrating = self._calibration_checker()

                try:
                    match = self._engine.detect(
                        captured_frame.detection_pcm16,
                        speech_present=_chunk_contains_speech(
                            captured_frame.detection_pcm16,
                            speech_detector,
                            detection_vad,
                        ),
                    )
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
                    if not self._publish_state_if_running("wakeword_detected"):
                        break
                    try:
                        outcome = self._handle_detection(tuple(detection_pre_roll))
                    except MicrophoneCaptureError:
                        logger.warning(
                            "Microphone capture failed during command recording; discarding audio",
                            exc_info=True,
                        )
                        detection_pre_roll.clear()
                        if self._restart_stream():
                            if not self._publish_state_if_running("listening"):
                                break
                            continue
                        if not self._recover_microphone("microphone_read_failed"):
                            break
                        continue
                    detection_pre_roll.clear()
                    if not self._running.is_set():
                        break
                    self._prepare_next_listen(outcome)
                    if not self._running.is_set():
                        break
                    if not self._restart_stream() and not self._recover_microphone(
                        "microphone_unavailable"
                    ):
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
        device = requested_device if isinstance(requested_device, dict) else None
        with _AUDIO_BACKEND_LOCK:
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
        self._publish_runtime_details_if_running(
            active_microphone={
                "index": capture_format.device,
                "name": capture_format.name,
                "host_api": capture_format.host_api,
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
            if not self._publish_state_if_running(outcome):
                return
            _sleep_while_running(self._running, _POST_DETECTION_LISTENING_HOLD_SECONDS)
        self._publish_state_if_running("listening")

    def _publish_state_if_running(
        self,
        state: str,
        error_code: str | None = None,
    ) -> bool:
        """Publish a state only while this worker still owns the Voice lifecycle."""
        with self._state_publish_lock:
            if not self._running.is_set():
                return False
            self._bridge.publish_state(state, error_code)
            return True

    def _publish_runtime_details_if_running(
        self,
        *,
        active_microphone: dict[str, object] | None,
    ) -> bool:
        """Publish runtime details only while this worker remains active."""
        with self._state_publish_lock:
            if not self._running.is_set():
                return False
            self._bridge.publish_runtime_details(active_microphone=active_microphone)
            return True

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

        self._stop_recording.clear()
        if not self._publish_state_if_running("recording"):
            return None
        audio_data = self._record_until_silence(pre_roll_audio)
        if not self._running.is_set():
            # Stopped (disabled/reconfigured) during recording — skip the network
            # round-trip entirely, no transcription and no send.
            return None
        if audio_data is None:
            return _OUTCOME_NO_SPEECH

        if not self._publish_state_if_running("transcribing"):
            return None
        self._close_stream()
        transcript = self._transcribe(audio_data)
        if not self._running.is_set():
            # Stopped during transcription — discard every result shape, including
            # failed or empty outcomes, so deliberate disable remains authoritative.
            logger.info("Wakeword worker stopped during transcription; discarding result")
            return None
        if transcript is None:
            logger.warning("Wakeword transcription failed; returning to listening")
            return _OUTCOME_TRANSCRIPTION_FAILED
        transcript = transcript.strip()
        if not transcript:
            logger.info("Wakeword recording produced no transcript; returning to listening")
            return _OUTCOME_NO_SPEECH
        if _is_voice_cancel_phrase(transcript):
            logger.info("Wakeword command discarded by voice cancel phrase")
            return _OUTCOME_CANCELLED

        if not self._publish_state_if_running("sending"):
            return None
        session_behavior = config.get("session_behavior", "active")

        session_id = self._resolve_session(agent_id, session_behavior)
        if not self._running.is_set():
            return None
        if not session_id:
            # A stop mid-resolve empties the result; that is not an error, so only
            # surface "error" when the worker is still meant to be running.
            if self._running.is_set():
                self._fail("session_resolution_failed")
            return None
        sent = self._send_transcript(transcript, agent_id, session_id)
        if not self._running.is_set():
            return None
        if not sent:
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
        with self._state_publish_lock:
            if not self._running.is_set():
                return
            logger.warning("Wakeword worker stopped (reason=%s)", error_code)
            self._running.clear()
            self._bridge.publish_state("error", error_code)

    def stop_recording(self) -> None:
        """End the active recording now, keeping the audio captured so far.

        The recording loop checks this event each frame and closes the capture
        early; the captured audio then flows through the normal transcription
        and send pipeline. The event is cleared before every recording starts,
        so a stop request never leaks into a later utterance.
        """
        self._stop_recording.set()

    def _recover_microphone(self, reason_code: str) -> bool:
        """Wait for a disconnected runtime microphone and reopen it when available."""
        logger.warning("Wakeword microphone disconnected (reason=%s)", reason_code)
        self._close_stream()
        if not self._publish_runtime_details_if_running(active_microphone=None):
            return False
        if not self._publish_state_if_running("microphone_disconnected", reason_code):
            return False
        while self._running.is_set():
            _sleep_while_running(
                self._running,
                _MICROPHONE_RECONNECT_INTERVAL_SECONDS,
            )
            if not self._running.is_set():
                return False
            refresh_microphone_devices()
            try:
                self._open_stream()
            except Exception:
                logger.debug("Microphone is still unavailable", exc_info=True)
                continue
            if not self._running.is_set():
                self._close_stream()
                return False
            logger.info("Wakeword microphone reconnected")
            return self._publish_state_if_running("listening")
        return False

    # -- Audio recording -----------------------------------------------------

    def _record_until_silence(
        self,
        pre_roll_audio: bytes | tuple[CapturedAudioFrame, ...] = b"",
    ) -> bytes | None:
        """Capture microphone audio until speech ends, then return the WAV bytes.

        Speech decisions come from the neural speech detector when it loaded;
        ambient noise (wind, rain, passing cars) then no longer counts as
        speech, so the recording closes at the real end of the utterance. When
        the neural model is unavailable the legacy WebRTC VAD keeps the old,
        noise-fragile behavior — degraded, but never silent. Returns ``None``
        when no frames were recorded.
        """
        detector = self._get_speech_detector()
        if detector is not None:
            detector.reset()
        fallback_vad = _create_recording_fallback_vad()
        frames: list[CapturedAudioFrame] = []
        pre_speech_frames: deque[CapturedAudioFrame] = deque(maxlen=_PRE_SPEECH_FRAME_COUNT)
        pre_speech_frames.extend(
            CapturedAudioFrame(frame, frame, _SAMPLE_RATE)
            for frame in _end_aligned_vad_frames(_detection_audio_bytes(pre_roll_audio))
        )
        silent_frames = 0
        has_speech = False
        waited_frames = 0
        upload_budget_bytes = self._resolve_upload_budget_bytes()
        recorded_bytes = 0

        while self._running.is_set():
            if self._stop_recording.is_set():
                logger.info("Wakeword recording stopped by user")
                break
            try:
                frame = _read_capture_frame(self._stream, _VAD_FRAME_SIZE)
            except Exception as exc:
                logger.warning("Microphone read error during recording", exc_info=True)
                raise MicrophoneCaptureError("Microphone failed during command recording") from exc

            is_speech = _frame_is_speech(frame, detector, fallback_vad)
            frame_bytes = len(frame.recording_pcm16)

            if has_speech and recorded_bytes + frame_bytes >= upload_budget_bytes:
                logger.warning(
                    "Wakeword recording reached the speech upload size budget; stopping capture"
                )
                break

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

            recorded_bytes += frame_bytes

            if has_speech and silent_frames >= _SILENCE_FRAME_COUNT:
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
                response = httpx.post(url, files=files, timeout=_HTTP_TIMEOUT, trust_env=False)
                if response.status_code == 200:
                    try:
                        result = response.json()
                    except ValueError:
                        logger.warning("Speech transcription returned invalid JSON")
                        return None
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
        """Return the newest session summary for an agent via session.list."""
        sessions = self._rpc_call(
            "session.list",
            {
                "agent_id": agent_id,
                "limit": 1,
                "include_subagents": True,
                "include_memory_reflections": True,
                "include_skill_reflections": True,
                "include_cron": True,
            },
        ).get("sessions", [])
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
        attempt_count = _MAX_RETRIES if method in _RETRYABLE_RPC_METHODS else 1

        for attempt in range(attempt_count):
            try:
                response = httpx.post(url, json=payload, timeout=_RPC_TIMEOUT, trust_env=False)
                if response.status_code == 200:
                    try:
                        rpc_response = response.json()
                    except ValueError:
                        logger.warning("RPC %s returned invalid JSON", method)
                        return {}
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
                if (
                    response.status_code in _RETRYABLE_STATUS_CODES
                    and attempt < attempt_count - 1
                    and self._running.is_set()
                ):
                    _backoff_sleep(attempt, self._running)
                    continue
                logger.warning("RPC %s failed: HTTP %s", method, response.status_code)
                return {}
            except httpx.RequestError:
                if attempt < attempt_count - 1 and self._running.is_set():
                    _backoff_sleep(attempt, self._running)
                    continue
                logger.warning("RPC %s request failed", method, exc_info=True)
                return {}
        return {}
