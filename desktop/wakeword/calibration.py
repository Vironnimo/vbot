"""Guided sensitivity calibration for one wake phrase.

A calibration run measures the room first, then the phrase:

1. ``noise``: for :data:`NOISE_SECONDS` the user stays quiet while the
   phrase's raw detector scores are collected; their
   :data:`NOISE_PERCENTILE` percentile becomes the noise level.
2. ``phrases``: the user says the phrase :data:`REQUIRED_SAMPLES` times. Each
   repetition is captured as one score peak: the score must first settle below
   a release level (the capture is armed), then cross the signal gate, and the
   peak is recorded once the score has fallen back below the release level for
   :data:`RELEASE_FRAMES` consecutive frames.
3. ``ready``: :func:`recommended_sensitivity` places a quantized threshold
   between the noise level and the median phrase peak.

A run ends by itself :data:`CALIBRATION_TIMEOUT_SECONDS` after it started; the
recommendation is only reported, never stored. The math is pure functions; the
state machine (:class:`PhraseCalibration`) takes explicit monotonic timestamps
and is not thread-safe: its owner serializes all calls.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from statistics import median
from typing import Any

from desktop.wakeword.config import MAX_SENSITIVITY, MIN_SENSITIVITY

logger = logging.getLogger("vbot.desktop.wakeword.calibration")

PHASE_NOISE = "noise"
PHASE_PHRASES = "phrases"
PHASE_READY = "ready"

CALIBRATION_TIMEOUT_SECONDS = 3 * 60
NOISE_SECONDS = 3.0
REQUIRED_SAMPLES = 5
RELEASE_FRAMES = 2
NOISE_PERCENTILE = 0.95
NOISE_MARGIN = 0.02
PHRASE_MARGIN = 0.02
THRESHOLD_GAP_RATIO = 0.5
SENSITIVITY_STEP = 0.05
NOISE_WARNING_LEVEL = 0.30


def percentile(values: list[float], fraction: float) -> float:
    """Return the linearly interpolated ``fraction`` percentile (``0.0`` when empty)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    weight = position - lower_index
    return ordered[lower_index] + ((ordered[upper_index] - ordered[lower_index]) * weight)


def recommended_sensitivity(noise_level: float, phrase_peaks: list[float]) -> float:
    """Place a quantized threshold safely between noise and the median phrase peak.

    The threshold keeps :data:`NOISE_MARGIN` above the noise level and
    :data:`PHRASE_MARGIN` below the median peak, and among the supported
    :data:`SENSITIVITY_STEP` steps in that window takes the one closest to the
    :data:`THRESHOLD_GAP_RATIO` point of the gap. Returns ``1 - threshold``.
    """
    reference_phrase = median(phrase_peaks) if phrase_peaks else 0.0
    separation = max(0.0, reference_phrase - noise_level)
    minimum_threshold = 1.0 - MAX_SENSITIVITY
    maximum_threshold = 1.0 - MIN_SENSITIVITY
    minimum_reliable_threshold = max(minimum_threshold, noise_level + NOISE_MARGIN)
    maximum_reliable_threshold = min(maximum_threshold, reference_phrase - PHRASE_MARGIN)
    target_threshold = noise_level + (separation * THRESHOLD_GAP_RATIO)
    supported_thresholds = [
        round(step * SENSITIVITY_STEP, 2)
        for step in range(1, 20)
        if minimum_reliable_threshold - 1e-9
        <= round(step * SENSITIVITY_STEP, 2)
        <= maximum_reliable_threshold + 1e-9
    ]
    if not supported_thresholds:
        threshold = max(minimum_threshold, min(maximum_threshold, maximum_reliable_threshold))
    else:
        threshold = min(
            supported_thresholds,
            key=lambda candidate: (abs(candidate - target_threshold), -candidate),
        )
    return round(1.0 - threshold, 2)


def calibration_signal_gate(noise_level: float) -> float:
    """Return the peak a repetition must reach: above the first noise-safe threshold step."""
    minimum_threshold = 1.0 - MAX_SENSITIVITY
    noise_safe_threshold = max(minimum_threshold, noise_level + NOISE_MARGIN)
    quantized_threshold = (
        math.ceil((noise_safe_threshold - 1e-9) / SENSITIVITY_STEP) * SENSITIVITY_STEP
    )
    return min(1.0, quantized_threshold + PHRASE_MARGIN)


class PhraseCalibration:
    """Calibration state machine for one wake phrase.

    Feed it every raw score frame of the running detector with
    :meth:`feed`; read the public status with :meth:`status`. Both advance
    the phase from ``noise`` to ``phrases`` once the noise window has passed.
    Once :meth:`expired`, :meth:`status` returns ``None`` and frames are
    ignored; the owner discards the instance.
    """

    def __init__(self, model_id: str, *, now: float) -> None:
        self._model_id = model_id
        self._deadline = 0.0
        self._noise_deadline = 0.0
        self._phase = PHASE_NOISE
        self._score = 0.0
        self._peak = 0.0
        self._noise_samples: list[float] = []
        self._noise_level: float | None = None
        self._sample_peaks: list[float] = []
        self._recommended_sensitivity: float | None = None
        self._candidate_peak = 0.0
        self._release_frames = 0
        self._armed = False
        self.restart(now)

    @property
    def model_id(self) -> str:
        """The calibrated phrase's model id."""
        return self._model_id

    @property
    def recommended_sensitivity(self) -> float | None:
        """The recommendation once the run is ``ready``, else ``None``."""
        return self._recommended_sensitivity

    def restart(self, now: float) -> None:
        """Start over from the noise phase with a fresh timeout."""
        self._deadline = now + CALIBRATION_TIMEOUT_SECONDS
        self._noise_deadline = now + NOISE_SECONDS
        self._phase = PHASE_NOISE
        self._score = 0.0
        self._peak = 0.0
        self._noise_samples = []
        self._noise_level = None
        self._sample_peaks = []
        self._recommended_sensitivity = None
        self._reset_capture()

    def expired(self, now: float) -> bool:
        """Whether the run has reached its timeout."""
        return now >= self._deadline

    def feed(self, scores: Mapping[str, float], now: float) -> None:
        """Advance the run with one raw score frame (a missing score counts as 0)."""
        if self.expired(now):
            return
        self._advance_phase(now)
        score = _normalized_score(scores.get(self._model_id, 0.0))
        self._score = score
        self._peak = max(self._peak, score)
        if self._phase == PHASE_NOISE:
            self._noise_samples.append(score)
        elif self._phase == PHASE_PHRASES:
            self._capture_sample(score)

    def status(self, now: float) -> dict[str, Any] | None:
        """Return the public calibration status, or ``None`` once expired."""
        if self.expired(now):
            return None
        self._advance_phase(now)
        in_noise_phase = self._phase == PHASE_NOISE
        return {
            "model_id": self._model_id,
            "phase": self._phase,
            "score": self._score,
            "peak": self._peak,
            "noise_level": self._noise_level,
            "noise_high": (
                self._noise_level is not None and self._noise_level >= NOISE_WARNING_LEVEL
            ),
            "sample_count": len(self._sample_peaks),
            "required_samples": REQUIRED_SAMPLES,
            "recommended_sensitivity": self._recommended_sensitivity,
            "noise_seconds_remaining": (
                max(0, math.ceil(self._noise_deadline - now)) if in_noise_phase else 0
            ),
        }

    def _advance_phase(self, now: float) -> None:
        if self._phase != PHASE_NOISE or now < self._noise_deadline:
            return
        self._noise_level = percentile(self._noise_samples, NOISE_PERCENTILE)
        self._phase = PHASE_PHRASES
        self._reset_capture()

    def _capture_sample(self, score: float) -> None:
        noise_level = self._noise_level or 0.0
        signal_gate = calibration_signal_gate(noise_level)
        release_level = min(
            signal_gate * 0.9,
            max(noise_level + (NOISE_MARGIN / 2), signal_gate * 0.6),
        )

        if self._candidate_peak > 0.0:
            self._candidate_peak = max(self._candidate_peak, score)
            if score < release_level:
                self._release_frames += 1
                if self._release_frames >= RELEASE_FRAMES:
                    self._record_sample(self._candidate_peak)
            else:
                self._release_frames = 0
            return

        if not self._armed:
            if score < release_level:
                self._release_frames += 1
                if self._release_frames >= RELEASE_FRAMES:
                    self._armed = True
                    self._release_frames = 0
            else:
                self._release_frames = 0
            return

        if score >= signal_gate:
            self._candidate_peak = score
            self._armed = False
            self._release_frames = 0

    def _record_sample(self, peak: float) -> None:
        self._sample_peaks.append(peak)
        self._reset_capture()
        if len(self._sample_peaks) < REQUIRED_SAMPLES:
            return
        self._recommended_sensitivity = recommended_sensitivity(
            self._noise_level or 0.0, self._sample_peaks
        )
        self._phase = PHASE_READY
        logger.info(
            "Wakeword calibration completed (model=%s, recommended_sensitivity=%.2f)",
            self._model_id,
            self._recommended_sensitivity,
        )

    def _reset_capture(self) -> None:
        self._candidate_peak = 0.0
        self._release_frames = 0
        self._armed = False


def _normalized_score(score: object) -> float:
    """Clamp a detector score to 0..1; non-numeric and non-finite scores count as 0."""
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return 0.0
    value = float(score)
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))
