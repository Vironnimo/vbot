"""Bridge: calibration behavior."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median

import pytest

from desktop.wakeword import _bridge_values as bridge_values
from desktop.wakeword import bridge as bridge_module
from desktop.wakeword.bridge import DesktopBridge
from desktop.wakeword.engine import (
    DEFAULT_WAKEWORD_MODEL_IDS,
    WakewordModelError,
)
from tests.desktop.bridge_helpers import (
    FakeWorker,
    _write_settings,
)


def test_guided_calibration_measures_noise_and_recommends_per_model_sensitivity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    worker = FakeWorker()
    worker.start()
    bridge = DesktopBridge(settings_path=settings_file, worker=worker)
    bridge.publish_state("listening")
    now = [100.0]
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: now[0])

    status = bridge.startWakewordCalibration()
    calibration = status["calibration"]
    assert calibration["active"] is True
    assert calibration["phase"] == "noise"
    assert calibration["noise_seconds_remaining"] == 3
    assert calibration["sample_counts"] == dict.fromkeys(DEFAULT_WAKEWORD_MODEL_IDS, 0)
    assert calibration["target_model_id"] is None

    now[0] = 100.5
    bridge.publish_calibration_scores(
        {
            DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.02,
            DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.01,
        }
    )
    now[0] = 101.5
    bridge.publish_calibration_scores(
        {
            DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.03,
            DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.02,
        }
    )

    now[0] = 103.1
    calibration = bridge.getWakewordStatus()["calibration"]
    assert calibration["phase"] == "phrases"
    assert calibration["target_model_id"] == DEFAULT_WAKEWORD_MODEL_IDS[0]
    assert calibration["noise_levels"] == pytest.approx(
        {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.0295, DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.0195}
    )

    def publish_target_score(model_id: str, score: float) -> None:
        bridge.publish_calibration_scores(
            {
                DEFAULT_WAKEWORD_MODEL_IDS[0]: (
                    score if model_id == DEFAULT_WAKEWORD_MODEL_IDS[0] else 0.01
                ),
                DEFAULT_WAKEWORD_MODEL_IDS[1]: (
                    score if model_id == DEFAULT_WAKEWORD_MODEL_IDS[1] else 0.01
                ),
            }
        )

    def capture_repetition(model_id: str, peak: float) -> None:
        publish_target_score(model_id, 0.01)
        publish_target_score(model_id, 0.01)
        publish_target_score(model_id, peak)
        publish_target_score(model_id, 0.01)
        publish_target_score(model_id, 0.01)

    for peak in (0.7, 0.6, 0.8, 0.65, 0.75):
        capture_repetition(DEFAULT_WAKEWORD_MODEL_IDS[0], peak)

    calibration = bridge.getWakewordStatus()["calibration"]
    assert calibration["sample_counts"] == {
        DEFAULT_WAKEWORD_MODEL_IDS[0]: 5,
        DEFAULT_WAKEWORD_MODEL_IDS[1]: 0,
    }
    assert calibration["target_model_id"] == DEFAULT_WAKEWORD_MODEL_IDS[1]
    assert calibration["recommended_sensitivities"] == {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.65}

    for peak in (0.65, 0.55, 0.75, 0.60, 0.70):
        capture_repetition(DEFAULT_WAKEWORD_MODEL_IDS[1], peak)

    calibration = bridge.getWakewordStatus()["calibration"]
    assert calibration["phase"] == "ready"
    assert calibration["target_model_id"] is None
    assert calibration["sample_counts"] == dict.fromkeys(DEFAULT_WAKEWORD_MODEL_IDS, 5)
    assert calibration["recommended_sensitivities"] == {
        DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.65,
        DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.65,
    }
    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "calibration" not in stored["wakeword"]

    restarted = bridge.restartWakewordCalibration()["calibration"]
    assert restarted["phase"] == "noise"
    assert restarted["scores"] == dict.fromkeys(DEFAULT_WAKEWORD_MODEL_IDS, 0.0)
    assert restarted["peaks"] == dict.fromkeys(DEFAULT_WAKEWORD_MODEL_IDS, 0.0)
    assert restarted["recommended_sensitivities"] == {}

    stopped = bridge.stopWakewordCalibration()["calibration"]
    assert stopped["active"] is False
    assert stopped["phase"] is None
    assert stopped["recommended_sensitivities"] == {}


@pytest.mark.parametrize(
    ("noise_level", "phrase_peaks"),
    [
        (0.0, [0.08, 0.1, 0.12, 0.09, 0.11]),
        (0.03, [0.6, 0.7, 0.8, 0.65, 0.75]),
        (0.31, [0.48, 0.55, 0.62, 0.50, 0.58]),
    ],
)
def test_calibrated_threshold_uses_supported_step_between_noise_and_phrase(
    noise_level: float,
    phrase_peaks: list[float],
) -> None:
    sensitivity = bridge_values._recommended_sensitivity(noise_level, phrase_peaks)
    threshold = 1.0 - sensitivity

    assert sensitivity / 0.05 == pytest.approx(round(sensitivity / 0.05))
    assert threshold >= noise_level + 0.02 - 1e-9
    reference_phrase = median(phrase_peaks)
    assert threshold <= reference_phrase - 0.02 + 1e-9


def test_calibration_requires_ready_real_listener(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    bridge = DesktopBridge(
        settings_path=settings_file,
        worker=FakeWorker(),
        mock=True,
    )
    bridge.publish_state("listening")

    with pytest.raises(RuntimeError):
        bridge.startWakewordCalibration()


def test_worker_stop_ends_calibration(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    worker = FakeWorker()
    worker.start()
    bridge = DesktopBridge(settings_path=settings_file, worker=worker)
    bridge.publish_state("listening")
    bridge.startWakewordCalibration()

    bridge.setWakewordEnabled(False)

    assert bridge.getWakewordStatus()["calibration"]["active"] is False


def test_retry_model_calibration_discards_one_model_samples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    worker = FakeWorker()
    worker.start()
    bridge = DesktopBridge(settings_path=settings_file, worker=worker)
    bridge.publish_state("listening")
    now = [100.0]
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: now[0])

    bridge.startWakewordCalibration()
    # Advance past noise phase
    now[0] = 100.5
    bridge.publish_calibration_scores(
        {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.02, DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.01}
    )
    now[0] = 101.5
    bridge.publish_calibration_scores(
        {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.03, DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.02}
    )
    now[0] = 103.1

    def capture_one(model_id: str, peak: float) -> None:
        publish_target_score(model_id, 0.01)
        publish_target_score(model_id, 0.01)
        publish_target_score(model_id, peak)
        publish_target_score(model_id, 0.01)
        publish_target_score(model_id, 0.01)

    def publish_target_score(model_id: str, score: float) -> None:
        bridge.publish_calibration_scores(
            {
                DEFAULT_WAKEWORD_MODEL_IDS[0]: (
                    score if model_id == DEFAULT_WAKEWORD_MODEL_IDS[0] else 0.01
                ),
                DEFAULT_WAKEWORD_MODEL_IDS[1]: (
                    score if model_id == DEFAULT_WAKEWORD_MODEL_IDS[1] else 0.01
                ),
            }
        )

    for peak in (0.7, 0.6, 0.8):
        capture_one(DEFAULT_WAKEWORD_MODEL_IDS[0], peak)

    assert bridge.getWakewordStatus()["calibration"]["sample_counts"] == {
        DEFAULT_WAKEWORD_MODEL_IDS[0]: 3,
        DEFAULT_WAKEWORD_MODEL_IDS[1]: 0,
    }

    # Retry model 0 — discard its 3 samples and reset to capture 5 fresh ones
    status = bridge.retryWakewordModelCalibration(DEFAULT_WAKEWORD_MODEL_IDS[0])
    assert status["calibration"]["sample_counts"] == {
        DEFAULT_WAKEWORD_MODEL_IDS[0]: 0,
        DEFAULT_WAKEWORD_MODEL_IDS[1]: 0,
    }
    assert status["calibration"]["target_model_id"] == DEFAULT_WAKEWORD_MODEL_IDS[0]
    assert status["calibration"]["phase"] == "phrases"
    assert DEFAULT_WAKEWORD_MODEL_IDS[0] not in status["calibration"]["recommended_sensitivities"]


def test_retry_model_calibration_rejects_unknown_model(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    worker = FakeWorker()
    worker.start()
    bridge = DesktopBridge(settings_path=settings_file, worker=worker)
    bridge.publish_state("listening")
    bridge.startWakewordCalibration()

    with pytest.raises(WakewordModelError):
        bridge.retryWakewordModelCalibration("builtin/alexa")


def test_calibration_reports_high_noise_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    worker = FakeWorker()
    worker.start()
    bridge = DesktopBridge(settings_path=settings_file, worker=worker)
    bridge.publish_state("listening")
    now = [100.0]
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: now[0])

    bridge.startWakewordCalibration()
    # Feed high noise scores
    now[0] = 100.5
    bridge.publish_calibration_scores(
        {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.35, DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.10}
    )
    now[0] = 101.5
    bridge.publish_calibration_scores(
        {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.40, DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.12}
    )
    now[0] = 103.1
    calibration = bridge.getWakewordStatus()["calibration"]
    assert calibration["phase"] == "phrases"
    assert calibration["noise_high"] is True


def test_calibration_noise_high_false_for_quiet_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_settings(settings_file, {"enabled": True})
    worker = FakeWorker()
    worker.start()
    bridge = DesktopBridge(settings_path=settings_file, worker=worker)
    bridge.publish_state("listening")
    now = [100.0]
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: now[0])

    bridge.startWakewordCalibration()
    now[0] = 100.5
    bridge.publish_calibration_scores(
        {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.02, DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.01}
    )
    now[0] = 101.5
    bridge.publish_calibration_scores(
        {DEFAULT_WAKEWORD_MODEL_IDS[0]: 0.03, DEFAULT_WAKEWORD_MODEL_IDS[1]: 0.02}
    )
    now[0] = 103.1
    calibration = bridge.getWakewordStatus()["calibration"]
    assert calibration["phase"] == "phrases"
    assert calibration["noise_high"] is False


def test_get_wakeword_status_includes_mock_flag(tmp_path: Path) -> None:
    _write_settings(tmp_path / "settings.json")

    real = DesktopBridge(settings_path=tmp_path / "settings.json")
    assert real.getWakewordStatus()["mock"] is False

    mock = DesktopBridge(settings_path=tmp_path / "settings.json", mock=True)
    assert mock.getWakewordStatus()["mock"] is True
