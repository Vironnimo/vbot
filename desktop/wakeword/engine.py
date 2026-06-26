"""Wakeword engine abstraction and implementations."""

from __future__ import annotations

from typing import Protocol


class WakewordEngine(Protocol):
    """Abstract interface for wakeword detection engines."""

    def start(self) -> None:
        """Initialize the engine and begin audio capture."""
        ...

    def stop(self) -> None:
        """Stop audio capture and release resources."""
        ...

    def detect(self, audio_chunk: bytes) -> float:
        """Return detection score 0.0-1.0 for an audio chunk."""
        ...


class MockWakewordEngine:
    """Configurable mock engine for UI testing without real microphone.

    Returns a configurable score sequence from ``detect`` so the WebUI can
    validate the full wakeword → recording → transcribing → sending flow when
    ``--mock-wakeword`` is used.
    """

    def __init__(self, score_sequence: list[float] | None = None) -> None:
        self._score_sequence = score_sequence or [0.0]
        self._index = 0
        self._running = False

    def start(self) -> None:
        self._running = True
        self._index = 0

    def stop(self) -> None:
        self._running = False

    def detect(self, audio_chunk: bytes) -> float:
        """Return the next score from the configured sequence."""
        if not self._running:
            return 0.0
        score = self._score_sequence[self._index % len(self._score_sequence)]
        self._index += 1
        return score

    def set_score_sequence(self, sequence: list[float]) -> None:
        """Replace the score sequence and reset the index."""
        self._score_sequence = list(sequence)
        self._index = 0


class OpenWakeWordEngine:
    """Wakeword detection via the openWakeWord library with ONNX inference.

    Loads a pre-trained openWakeWord model for the configured wake phrase.
    The model expects 16kHz mono 16-bit PCM audio in 1280-sample (80ms) chunks.
    Sensitivity maps to score threshold: ``threshold = 1.0 - sensitivity``.
    """

    def __init__(self, wake_phrase: str = "hey_jarvis", sensitivity: float = 0.5) -> None:
        self._wake_phrase = wake_phrase
        self._sensitivity = float(sensitivity)
        self._threshold = max(0.0, min(1.0, 1.0 - self._sensitivity))
        self._model = None

    @property
    def threshold(self) -> float:
        """Score threshold above which a detection is triggered."""
        return self._threshold

    def start(self) -> None:
        """Load the openWakeWord model for inference."""
        from openwakeword.model import Model  # type: ignore[import-untyped]

        self._model = Model(
            wakeword_models=[self._wake_phrase],
            inference_framework="onnx",
        )

    def stop(self) -> None:
        """Release the openWakeWord model."""
        self._model = None

    def detect(self, audio_chunk: bytes) -> float:
        """Run inference on an audio chunk and return the detection score."""
        if self._model is None:
            return 0.0
        import numpy as np

        audio_array = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32)
        prediction = self._model.predict(audio_array)
        score = float(prediction.get(self._wake_phrase, 0.0))
        return max(0.0, min(1.0, score))
