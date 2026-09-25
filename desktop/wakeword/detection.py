"""Continuous wake phrase detection over one capture subscription.

:class:`DetectionLoop` runs on its own daemon thread (``vbot-voice-detection``)
for the lifetime of one listener. It starts the engine on that thread, re-chunks
the 16 kHz projection of every captured block into the engine's 80 ms chunks,
gates each chunk's scores with its own speech detector, and reports every
winning phrase as a :class:`Detection` with the preceding audio (at least
:data:`PRE_ROLL_SECONDS`, in whole capture blocks) as pre-roll.

The loop never waits for anything a detection starts: ``on_detection`` runs
synchronously and must return promptly (the command recorder reads its own
subscription). While ``calibrating()`` is true, matches are suppressed; the
engine's score listener keeps receiving every score frame.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from desktop.wakeword._speech_detection import SpeechDetector, chunk_contains_speech
from desktop.wakeword.capture import AudioBlock, CaptureGap, CaptureSubscription
from desktop.wakeword.engine import WakewordEngine

logger = logging.getLogger("vbot.desktop.wakeword.detection")

DETECTION_CHUNK_SAMPLES = 1280
"""Samples (80 ms at 16 kHz) the engine scores per call."""

PRE_ROLL_SECONDS = 0.32
"""Minimum audio before a detection handed to the command recorder."""

SUBSCRIPTION_SECONDS = 2.0
"""Queue bound of the detection subscription."""

ERROR_ENGINE_START_FAILED = "engine_start_failed"
ERROR_DETECTION_FAILED = "detection_failed"
ERROR_PIPELINE_FAILED = "pipeline_failed"

_CHUNK_BYTES = DETECTION_CHUNK_SAMPLES * 2
_READ_TIMEOUT_SECONDS = 0.1


@dataclass(frozen=True)
class Detection:
    """One wake phrase the engine recognized."""

    model_id: str
    score: float
    threshold: float
    pre_roll: tuple[AudioBlock, ...]
    """The capture blocks up to and including the one that completed the phrase."""


class DetectionLoop:
    """Runs one engine over one subscription until ``stop_event`` is set.

    ``on_started`` is called once the engine has loaded; ``on_failed(code)``
    when it cannot start (``engine_start_failed``) or fails while detecting
    (``detection_failed``, ``pipeline_failed``). The thread stops the engine
    and closes the subscription itself.
    """

    def __init__(
        self,
        *,
        subscription: CaptureSubscription,
        engine: WakewordEngine,
        stop_event: threading.Event,
        on_detection: Callable[[Detection], None],
        on_started: Callable[[], None],
        on_failed: Callable[[str], None],
        calibrating: Callable[[], bool],
        speech_detector_factory: Callable[[], SpeechDetector | None],
        fallback_vad_factory: Callable[[], Any | None],
    ) -> None:
        self._subscription = subscription
        self._engine = engine
        self._stop = stop_event
        self._on_detection = on_detection
        self._on_started = on_started
        self._on_failed = on_failed
        self._calibrating = calibrating
        self._speech_detector_factory = speech_detector_factory
        self._fallback_vad_factory = fallback_vad_factory
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the detection thread (once)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="vbot-voice-detection", daemon=True)
        self._thread.start()

    def join(self, timeout: float) -> bool:
        """Wait for the thread after ``stop_event``; ``True`` once it ended."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        try:
            if self._stop.is_set():
                return
            try:
                self._engine.start()
            except Exception:
                logger.warning("Wakeword engine could not start", exc_info=True)
                self._on_failed(ERROR_ENGINE_START_FAILED)
                return
            try:
                self._detect()
            finally:
                try:
                    self._engine.stop()
                except Exception:
                    logger.warning("Wakeword engine did not stop cleanly", exc_info=True)
        except Exception:
            logger.exception("Wakeword detection stopped unexpectedly")
            self._on_failed(ERROR_PIPELINE_FAILED)
        finally:
            self._subscription.close()

    def _detect(self) -> None:
        speech_detector = self._speech_detector_factory()
        fallback_vad = self._fallback_vad_factory()
        if self._stop.is_set():
            return
        self._on_started()
        pending = bytearray()
        pre_roll: deque[AudioBlock] = deque()
        pre_roll_seconds = 0.0
        while not self._stop.is_set():
            item = self._subscription.read(_READ_TIMEOUT_SECONDS)
            if item is None:
                if self._subscription.closed:
                    return
                continue
            if isinstance(item, CaptureGap):
                # The audio around a gap does not belong together.
                pending.clear()
                pre_roll.clear()
                pre_roll_seconds = 0.0
                if speech_detector is not None:
                    speech_detector.reset()
                continue
            pre_roll.append(item)
            pre_roll_seconds += item.duration
            while len(pre_roll) > 1 and pre_roll_seconds - pre_roll[0].duration >= PRE_ROLL_SECONDS:
                pre_roll_seconds -= pre_roll.popleft().duration
            pending += item.pcm16
            while len(pending) >= _CHUNK_BYTES:
                chunk = bytes(pending[:_CHUNK_BYTES])
                del pending[:_CHUNK_BYTES]
                speech_present = chunk_contains_speech(chunk, speech_detector, fallback_vad)
                try:
                    match = self._engine.detect(chunk, speech_present=speech_present)
                except Exception:
                    logger.warning("Wakeword detection failed", exc_info=True)
                    self._on_failed(ERROR_DETECTION_FAILED)
                    return
                if match is None:
                    continue
                if self._calibrating():
                    continue
                logger.info(
                    "Wakeword detected: model=%s score=%.3f threshold=%.3f",
                    match.model_id,
                    match.score,
                    match.threshold,
                )
                detection = Detection(match.model_id, match.score, match.threshold, tuple(pre_roll))
                pre_roll.clear()
                pre_roll_seconds = 0.0
                try:
                    self._on_detection(detection)
                except Exception:
                    logger.exception("Wakeword detection handler failed")
