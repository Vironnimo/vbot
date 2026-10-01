"""Speech detection: the neural speech detector and the delayed :class:`SpeechGate`
for wakeword scores.

Every consumer (the detection gate, command endpointing) creates its own
:class:`SpeechDetector` because the model keeps per-stream state. Without a
working detector every speech decision fails open: all audio counts as speech,
so a technical failure never mutes Voice. All audio is 16 kHz mono PCM16.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("vbot.desktop.wakeword.speech_detection")

SPEECH_SAMPLE_RATE = 16000
"""Rate of all audio this module judges."""

SPEECH_HOP_SAMPLES = 512
"""One neural detector hop (32 ms), the recording endpointing frame size."""

# Silero v5 consumes strict 512-sample hops at 16 kHz with a 64-sample leading
# context; the detector buffers partial hops for callers feeding other sizes.
_SPEECH_VAD_CONTEXT_SAMPLES = 64
_SPEECH_PROB_THRESHOLD = 0.5  # Silero's canonical speech threshold
_SPEECH_PROB_NEG_THRESHOLD = 0.35  # exit threshold (threshold - 0.15)

# Upstream openWakeWord gates a chunk's scores on the speech decisions of the
# chunks 4 to 6 before it (0.32-0.56 s earlier): the heads peak after the phrase.
_GATE_NEAREST_CHUNK = 4
_GATE_FARTHEST_CHUNK = 6


class SpeechDetector:
    """Neural speech-or-noise decision for endpointing and detection gating.

    Runs the bundled Silero VAD v5 ONNX model over 32 ms windows at 16 kHz and
    answers a binary question per window: does this audio carry human speech,
    or is it ambient noise (wind, rain, traffic, music)? The decision is
    amplitude- and noise-robust, which is what keeps the recording channel
    from being held open by continuous noise.

    Loading can fail (onnxruntime or the model file absent); the caller treats
    the detector factory's ``None`` result as fail-open.

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
                "Neural speech detector unavailable; all audio counts as speech",
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
        if len(samples) == SPEECH_HOP_SAMPLES:
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
        while consumed + SPEECH_HOP_SAMPLES <= len(buffered):
            hop = buffered[consumed : consumed + SPEECH_HOP_SAMPLES]
            probability = self._score_window(np.concatenate([self._context, hop]))
            max_probability = max(max_probability, probability)
            consumed += SPEECH_HOP_SAMPLES
        self._pending = np.array(buffered[consumed:], dtype=np.float32)
        return max_probability

    def _score_window(self, window: np.ndarray) -> float:
        """Run one context-padded 512-sample window through the model."""
        out, state = self._session.run(
            None,
            {
                "input": window.reshape(1, -1).astype(np.float32),
                "state": self._state,
                "sr": np.array(SPEECH_SAMPLE_RATE, dtype=np.int64),
            },
        )
        self._state = np.asarray(state, dtype=np.float32)
        self._context = window[-_SPEECH_VAD_CONTEXT_SAMPLES:]
        probability: float = float(np.asarray(out).item())
        return probability


def frame_is_speech(pcm16: bytes, detector: SpeechDetector | None) -> bool:
    """Decide whether one 32 ms (512-sample) frame carries speech, with a fail-open bias.

    The neural detector decides (with hysteresis across frames). Without it, or
    on an unexpected scoring error, the frame counts as speech so a technical
    failure can never mute recording.
    """
    if detector is None:
        return True
    try:
        return detector.is_speech(pcm16)
    except Exception:
        logger.warning("Neural speech scoring failed; counting the frame as speech", exc_info=True)
        return True


def chunk_contains_speech(detection_pcm16: bytes, speech_detector: SpeechDetector | None) -> bool:
    """Whether one detection chunk carries enough speech to trust model scores.

    Uses the neural speech detector: ambient noise must not open the gate, or
    wakeword scores would accumulate toward false activations in wind and rain.
    Fails open without a detector or on a scoring error: the gate can never
    turn into an accidental mute.
    """
    if speech_detector is None:
        return True
    try:
        return speech_detector.speech_probability(detection_pcm16) >= _SPEECH_PROB_THRESHOLD
    except Exception:
        logger.warning("Neural speech scoring failed; counting the chunk as speech", exc_info=True)
        return True


class SpeechGate:
    """Decides per 80 ms detection chunk whether its wakeword scores count.

    Mirrors upstream openWakeWord's VAD threshold: a chunk's scores count when
    any of the chunks 4 to 6 before it carried speech (see
    :func:`chunk_contains_speech`), because the heads score a phrase highest
    0.2-0.5 s after it ended, when the current chunk is already silent. Like
    upstream, the gate stays closed for the first four chunks after creation or
    :meth:`reset`, and the fifth and sixth consult the shorter history they have.
    Without any speech detector the gate is open, as upstream without VAD.
    """

    def __init__(self, speech_detector: SpeechDetector | None) -> None:
        self._detector = speech_detector
        self._speech: deque[bool] = deque(maxlen=_GATE_FARTHEST_CHUNK + 1)

    def admits(self, detection_pcm16: bytes) -> bool:
        """Record this chunk's speech decision and return whether its scores count."""
        if self._detector is None:
            return True
        self._speech.append(chunk_contains_speech(detection_pcm16, self._detector))
        return any(list(self._speech)[:-_GATE_NEAREST_CHUNK])

    def reset(self) -> None:
        """Forget the speech history, e.g. when the audio around a capture gap does not connect."""
        self._speech.clear()
        if self._detector is not None:
            self._detector.reset()
