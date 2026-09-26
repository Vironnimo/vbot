"""Tests for guided wake phrase calibration (``desktop.wakeword.calibration``)."""

from __future__ import annotations

from statistics import median

import pytest

from desktop.wakeword.calibration import (
    CALIBRATION_TIMEOUT_SECONDS,
    NOISE_SECONDS,
    REQUIRED_SAMPLES,
    PhraseCalibration,
    calibration_signal_gate,
    percentile,
    recommended_sensitivity,
)

MODEL = "builtin/okay_nabu"
OTHER = "builtin/hey_nabu"
START = 100.0


def _quiet_room(calibration: PhraseCalibration) -> None:
    """Feed the noise phase two quiet frames, then leave it."""
    calibration.feed({MODEL: 0.02, OTHER: 0.5}, START + 0.5)
    calibration.feed({MODEL: 0.03, OTHER: 0.5}, START + 1.5)


def _say_phrase(calibration: PhraseCalibration, peak: float, now: float) -> None:
    """One repetition: settle, peak, fall back below the release level."""
    for score in (0.01, 0.01, peak, 0.01, 0.01):
        calibration.feed({MODEL: score}, now)


def test_guided_calibration_measures_noise_then_recommends_a_sensitivity() -> None:
    calibration = PhraseCalibration(MODEL, now=START)

    status = calibration.status(START)
    assert status == {
        "model_id": MODEL,
        "phase": "noise",
        "score": 0.0,
        "peak": 0.0,
        "noise_level": None,
        "noise_high": False,
        "sample_count": 0,
        "required_samples": REQUIRED_SAMPLES,
        "recommended_sensitivity": None,
        "noise_seconds_remaining": 3,
    }
    _quiet_room(calibration)
    noise_status = calibration.status(START + 1.5)
    assert noise_status is not None
    assert noise_status["noise_seconds_remaining"] == 2
    assert noise_status["score"] == pytest.approx(0.03)

    now = START + NOISE_SECONDS + 0.1
    phrase_status = calibration.status(now)
    assert phrase_status is not None
    assert phrase_status["phase"] == "phrases"
    assert phrase_status["noise_level"] == pytest.approx(0.0295)
    assert phrase_status["noise_high"] is False
    assert phrase_status["noise_seconds_remaining"] == 0

    for index, peak in enumerate((0.7, 0.6, 0.8, 0.65, 0.75), start=1):
        _say_phrase(calibration, peak, now)
        progress = calibration.status(now)
        assert progress is not None
        assert progress["sample_count"] == index

    ready = calibration.status(now)
    assert ready is not None
    assert ready["phase"] == "ready"
    assert ready["peak"] == pytest.approx(0.8)
    assert ready["recommended_sensitivity"] == 0.65
    assert calibration.recommended_sensitivity == 0.65


def test_restart_begins_again_with_the_noise_phase() -> None:
    calibration = PhraseCalibration(MODEL, now=START)
    _quiet_room(calibration)
    now = START + NOISE_SECONDS + 0.1
    for peak in (0.7, 0.6, 0.8, 0.65, 0.75):
        _say_phrase(calibration, peak, now)
    assert calibration.recommended_sensitivity is not None

    calibration.restart(now)

    assert calibration.recommended_sensitivity is None
    assert calibration.status(now) == {
        "model_id": MODEL,
        "phase": "noise",
        "score": 0.0,
        "peak": 0.0,
        "noise_level": None,
        "noise_high": False,
        "sample_count": 0,
        "required_samples": REQUIRED_SAMPLES,
        "recommended_sensitivity": None,
        "noise_seconds_remaining": 3,
    }
    assert calibration.expired(now + CALIBRATION_TIMEOUT_SECONDS - 1) is False


def test_calibration_expires_after_its_timeout() -> None:
    calibration = PhraseCalibration(MODEL, now=START)
    deadline = START + CALIBRATION_TIMEOUT_SECONDS

    assert calibration.expired(deadline - 0.1) is False
    assert calibration.status(deadline - 0.1) is not None
    assert calibration.expired(deadline) is True
    assert calibration.status(deadline) is None
    calibration.feed({MODEL: 0.9}, deadline)
    assert calibration.status(deadline - 0.1) is not None
    assert calibration.status(deadline - 0.1)["peak"] == 0.0  # type: ignore[index]


@pytest.mark.parametrize(
    ("noise_scores", "noise_high"),
    [((0.35, 0.40), True), ((0.02, 0.03), False)],
)
def test_calibration_reports_a_noisy_room(
    noise_scores: tuple[float, float], noise_high: bool
) -> None:
    calibration = PhraseCalibration(MODEL, now=START)
    calibration.feed({MODEL: noise_scores[0]}, START + 0.5)
    calibration.feed({MODEL: noise_scores[1]}, START + 1.5)

    during_noise = calibration.status(START + 2.0)
    after_noise = calibration.status(START + NOISE_SECONDS)

    assert during_noise is not None
    assert during_noise["noise_high"] is False
    assert after_noise is not None
    assert after_noise["phase"] == "phrases"
    assert after_noise["noise_high"] is noise_high


def test_a_repetition_counts_only_after_the_score_settles() -> None:
    calibration = PhraseCalibration(MODEL, now=START)
    _quiet_room(calibration)
    now = START + NOISE_SECONDS

    # Still high from before the phrase phase: not armed, so no sample.
    for score in (0.8, 0.8, 0.5):
        calibration.feed({MODEL: score}, now)
    # Scores below the signal gate never start a repetition.
    for score in (0.01, 0.01, 0.05, 0.01, 0.01):
        calibration.feed({MODEL: score}, now)
    status = calibration.status(now)
    assert status is not None
    assert status["sample_count"] == 0

    _say_phrase(calibration, 0.8, now)

    status = calibration.status(now)
    assert status is not None
    assert status["sample_count"] == 1


def test_a_repetition_records_its_highest_score() -> None:
    calibration = PhraseCalibration(MODEL, now=START)
    _quiet_room(calibration)
    now = START + NOISE_SECONDS
    noise_level = calibration.status(now)["noise_level"]  # type: ignore[index]

    for _ in range(REQUIRED_SAMPLES):
        for score in (0.01, 0.01, 0.3, 0.9, 0.5, 0.01, 0.01):
            calibration.feed({MODEL: score}, now)

    assert calibration.recommended_sensitivity == recommended_sensitivity(
        noise_level, [0.9] * REQUIRED_SAMPLES
    )


@pytest.mark.parametrize(
    "scores",
    [{MODEL: float("nan")}, {MODEL: float("inf")}, {MODEL: -1.0}, {}, {OTHER: 0.9}],
)
def test_unusable_scores_count_as_silence(scores: dict[str, float]) -> None:
    calibration = PhraseCalibration(MODEL, now=START)

    calibration.feed(scores, START + 0.5)

    status = calibration.status(START + 0.5)
    assert status is not None
    assert status["score"] == 0.0
    assert status["peak"] == 0.0


def test_scores_above_one_are_clamped() -> None:
    calibration = PhraseCalibration(MODEL, now=START)

    calibration.feed({MODEL: 3.0}, START + 0.5)

    assert calibration.status(START + 0.5)["peak"] == 1.0  # type: ignore[index]


# -- Pure math -----------------------------------------------------------------


def test_percentile_interpolates_between_ordered_values() -> None:
    assert percentile([], 0.95) == 0.0
    assert percentile([0.4], 0.95) == 0.4
    assert percentile([0.03, 0.02], 0.95) == pytest.approx(0.0295)
    assert percentile([0.1, 0.2, 0.3], 0.5) == pytest.approx(0.2)


@pytest.mark.parametrize(
    ("noise_level", "phrase_peaks"),
    [
        (0.0, [0.08, 0.1, 0.12, 0.09, 0.11]),
        (0.03, [0.6, 0.7, 0.8, 0.65, 0.75]),
        (0.31, [0.48, 0.55, 0.62, 0.50, 0.58]),
    ],
)
def test_recommended_threshold_uses_a_supported_step_between_noise_and_phrase(
    noise_level: float,
    phrase_peaks: list[float],
) -> None:
    sensitivity = recommended_sensitivity(noise_level, phrase_peaks)
    threshold = 1.0 - sensitivity

    assert sensitivity / 0.05 == pytest.approx(round(sensitivity / 0.05))
    assert threshold >= noise_level + 0.02 - 1e-9
    assert threshold <= median(phrase_peaks) - 0.02 + 1e-9


def test_recommendation_without_room_between_noise_and_phrase_is_most_sensitive() -> None:
    assert recommended_sensitivity(0.0, [0.05] * REQUIRED_SAMPLES) == 0.95


@pytest.mark.parametrize(("noise_level", "gate"), [(0.0, 0.07), (0.0295, 0.07), (0.31, 0.37)])
def test_signal_gate_sits_above_the_first_noise_safe_threshold_step(
    noise_level: float, gate: float
) -> None:
    assert calibration_signal_gate(noise_level) == pytest.approx(gate)
