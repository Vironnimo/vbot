"""Speech detection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

from desktop.wakeword._audio_capture import (
    CapturedAudioFrame,
)
from desktop.wakeword._worker_constants import (
    _DETECTION_VAD_FRAME_BYTES,
    _DETECTION_VAD_MIN_SPEECH_FRAMES,
    _SAMPLE_RATE,
    _SPEECH_PROB_NEG_THRESHOLD,
    _SPEECH_PROB_THRESHOLD,
    _SPEECH_VAD_CONTEXT_SAMPLES,
    _SPEECH_VAD_HOP_SAMPLES,
    _SPEECH_VAD_SAMPLE_RATE,
    _VAD_MODE,
    logger,
)


class SpeechDetector:
    """Neural speech-or-noise decision for endpointing and detection gating.

    Runs the bundled Silero VAD v5 ONNX model over 32 ms windows at 16 kHz and
    answers a binary question per window: does this audio carry human speech,
    or is it ambient noise (wind, rain, traffic, music)? Unlike the WebRTC VAD
    fallback this decision is amplitude- and noise-robust, which is what keeps
    the recording channel from being held open by continuous noise.

    Loading can fail (onnxruntime or the model file absent); the caller treats
    the detector factory's ``None`` result as fail-open, exactly like the
    legacy WebRTC gate.

    The binary decision applies hysteresis: speech opens at
    ``_SPEECH_PROB_THRESHOLD`` and only closes below ``_SPEECH_PROB_NEG_THRESHOLD``,
    so a word-internal dip never splits an utterance. ``reset()`` re-arms the
    opening threshold for the next utterance.
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(_SPEECH_VAD_CONTEXT_SAMPLES, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._active = False

    @classmethod
    def create(cls) -> SpeechDetector | None:
        """Load the bundled model, returning ``None`` when the stack is absent."""
        try:
            import onnxruntime

            model_path = Path(__file__).with_name("models") / "silero_vad.onnx"
            options = onnxruntime.SessionOptions()
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            options.log_severity_level = 3
            session = onnxruntime.InferenceSession(
                os.fspath(model_path),
                providers=["CPUExecutionProvider"],
                sess_options=options,
            )
            return cls(session)
        except Exception:
            logger.warning(
                "Neural speech detector unavailable; WebRTC VAD fallback stays active",
                exc_info=True,
            )
            return None

    def reset(self) -> None:
        """Clear model state so a new utterance starts from a clean history."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(_SPEECH_VAD_CONTEXT_SAMPLES, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._active = False

    def is_speech(self, pcm16: bytes) -> bool:
        """Whether one 32 ms VAD frame still belongs to an active utterance.

        Accepts one 512-sample PCM16 frame per call (the recording loop's
        frame size, one Silero inference hop); internally the 64-sample
        leading context is prepended. Other chunk sizes go through the
        buffered ``probability`` path instead.
        """
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) == _SPEECH_VAD_HOP_SAMPLES:
            probability = self._score_window(np.concatenate([self._context, samples]))
        else:
            probability = self.probability(samples)
        if self._active:
            if probability >= _SPEECH_PROB_NEG_THRESHOLD:
                return True
            self._active = False
            return False
        if probability >= _SPEECH_PROB_THRESHOLD:
            self._active = True
            return True
        return False

    def speech_probability(self, detection_pcm16: bytes) -> float:
        """Score one 80 ms detection chunk on the 0..1 probability scale."""
        samples = np.frombuffer(detection_pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        return self.probability(samples)

    def probability(self, samples_16k: np.ndarray) -> float:
        """Score arbitrary 16 kHz float samples, returning the maximum window probability.

        Partial hops are buffered inside the detector, so callers may feed any
        chunk size; the model only ever sees complete 512-sample hops.
        """
        if self._session is None or len(samples_16k) == 0:
            return 0.0
        buffered = np.concatenate([self._pending, samples_16k])
        max_probability = 0.0
        consumed = 0
        while consumed + _SPEECH_VAD_HOP_SAMPLES <= len(buffered):
            hop = buffered[consumed : consumed + _SPEECH_VAD_HOP_SAMPLES]
            probability = self._score_window(np.concatenate([self._context, hop]))
            max_probability = max(max_probability, probability)
            consumed += _SPEECH_VAD_HOP_SAMPLES
        self._pending = np.array(buffered[consumed:], dtype=np.float32)
        return max_probability

    def _score_window(self, window: np.ndarray) -> float:
        """Run one context-padded 512-sample window through the model."""
        out, state = self._session.run(
            None,
            {
                "input": window.reshape(1, -1).astype(np.float32),
                "state": self._state,
                "sr": np.array(_SPEECH_VAD_SAMPLE_RATE, dtype=np.int64),
            },
        )
        self._state = np.asarray(state, dtype=np.float32)
        self._context = window[-_SPEECH_VAD_CONTEXT_SAMPLES:]
        probability: float = float(np.asarray(out).item())
        return probability


def _create_recording_fallback_vad() -> Any | None:
    """Create the legacy WebRTC VAD used when the neural detector is absent."""
    try:
        import webrtcvad  # type: ignore[import-untyped]

        return webrtcvad.Vad(_VAD_MODE)
    except Exception:
        logger.warning("WebRTC fallback VAD unavailable", exc_info=True)
        return None


def _frame_is_speech(
    frame: CapturedAudioFrame,
    detector: SpeechDetector | None,
    fallback_vad: Any | None,
) -> bool:
    """Decide whether one 30 ms frame carries speech, with a fail-open bias.

    The neural detector is authoritative when present. Without it (or on an
    unexpected scoring error) the WebRTC fallback decides; a totally unavailable
    stack counts frames as speech so a technical failure can never mute
    recording — the worst case is today's noise-fragile behavior.
    """
    if detector is not None:
        try:
            return detector.is_speech(frame.detection_pcm16)
        except Exception:
            logger.warning("Neural speech scoring failed; using WebRTC fallback", exc_info=True)
    if fallback_vad is None:
        return True
    try:
        verdict: bool = bool(fallback_vad.is_speech(frame.detection_pcm16, _SAMPLE_RATE))
    except Exception:
        return True
    return verdict


def _create_detection_vad() -> Any | None:
    """Create the VAD that gates detection scores, or None when unavailable."""
    try:
        import webrtcvad  # type: ignore[import-untyped]

        return webrtcvad.Vad(_VAD_MODE)
    except Exception:
        # A missing or broken VAD must not silently disable wake word
        # detection — the gate fails open and scores stay ungated.
        logger.warning("Detection VAD unavailable; wakeword scores stay ungated", exc_info=True)
        return None


def _chunk_contains_speech(
    detection_pcm16: bytes,
    speech_detector: SpeechDetector | None,
    fallback_vad: Any | None,
) -> bool:
    """Whether one detection chunk carries enough speech to trust model scores.

    Prefers the neural speech detector: ambient noise must not open the gate,
    or wakeword scores would accumulate toward false activations in wind and
    rain. Falls back to the legacy WebRTC VAD when no neural detector loaded,
    keeping the previous 20 ms speech-slices rule. Both paths fail open — the
    gate can never turn into an accidental mute.
    """
    if speech_detector is not None:
        try:
            return speech_detector.speech_probability(detection_pcm16) >= _SPEECH_PROB_THRESHOLD
        except Exception:
            logger.warning("Neural speech scoring failed; using WebRTC fallback", exc_info=True)
    if not fallback_vad or len(detection_pcm16) < _DETECTION_VAD_FRAME_BYTES:
        return True
    speech_frames = 0
    frame_count = len(detection_pcm16) // _DETECTION_VAD_FRAME_BYTES
    for frame_index in range(frame_count):
        offset = frame_index * _DETECTION_VAD_FRAME_BYTES
        try:
            if fallback_vad.is_speech(
                detection_pcm16[offset : offset + _DETECTION_VAD_FRAME_BYTES],
                _SAMPLE_RATE,
            ):
                speech_frames += 1
        except Exception:
            return True
        if speech_frames >= _DETECTION_VAD_MIN_SPEECH_FRAMES:
            return True
    return False
