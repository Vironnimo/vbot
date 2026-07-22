"""Tests for the Desktop wakeword model catalog and openWakeWord adapter."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from desktop.wakeword import engine as engine_module
from desktop.wakeword.engine import (
    DEFAULT_WAKEWORD_MODEL_ID,
    MAX_CUSTOM_WAKEWORD_MODEL_BYTES,
    MAX_WAKEWORD_SENSITIVITY,
    MIN_WAKEWORD_SENSITIVITY,
    OpenWakeWordEngine,
    PyOpenWakeWordEngine,
    WakewordModelCatalog,
    WakewordModelError,
)


def test_catalog_lists_only_curated_public_builtin_models(tmp_path: Path) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    models = [model.to_dict() for model in catalog.list_models()]

    assert [model["id"] for model in models] == [
        "builtin/okay_nabu",
        "builtin/hey_jarvis",
        "builtin/hey_mycroft",
        "builtin/hey_rhasspy",
        "builtin/alexa",
    ]
    assert models[0] == {
        "id": "builtin/okay_nabu",
        "label": "Okay Nabu",
        "source": "built_in",
        "format": "tflite",
        "removable": False,
    }
    assert all("target" not in model for model in models)
    assert all("backend" not in model for model in models)
    assert catalog.resolve(DEFAULT_WAKEWORD_MODEL_ID).target == "hey_jarvis"


def test_catalog_imports_and_resolves_a_valid_custom_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "_validate_custom_model", Mock())
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    imported = catalog.import_model(r"C:\downloads\hey_computer-v1.onnx", b"onnx-bytes")

    assert imported.id.startswith("custom/")
    assert imported.label == "hey computer v1"
    assert imported.removable is True
    assert Path(imported.target).read_bytes() == b"onnx-bytes"
    assert catalog.resolve(imported.id) == imported
    metadata = json.loads(Path(imported.target).with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["id"] == imported.id
    assert metadata["filename"] == Path(imported.target).name


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("model.tflite", b"data", "ONNX"),
        ("model.onnx", b"", "empty"),
        ("", b"data", "filename"),
    ],
)
def test_catalog_rejects_invalid_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    content: bytes,
    message: str,
) -> None:
    monkeypatch.setattr(engine_module, "_validate_custom_model", Mock())
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    with pytest.raises(WakewordModelError, match=message):
        catalog.import_model(filename, content)


def test_catalog_rejects_oversized_import(tmp_path: Path) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    with pytest.raises(WakewordModelError, match="exceeds"):
        catalog.import_model("large.onnx", b"x" * (MAX_CUSTOM_WAKEWORD_MODEL_BYTES + 1))


def test_catalog_cleans_up_a_model_that_fails_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    def reject(_path: Path) -> None:
        raise WakewordModelError("bad model")

    monkeypatch.setattr(engine_module, "_validate_custom_model", reject)

    with pytest.raises(WakewordModelError, match="bad model"):
        catalog.import_model("bad.onnx", b"not-onnx")

    assert list((tmp_path / "wakewords").iterdir()) == []


def test_catalog_deletes_only_imported_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "_validate_custom_model", Mock())
    catalog = WakewordModelCatalog(tmp_path / "settings.json")
    imported = catalog.import_model("computer.onnx", b"onnx")

    catalog.delete_model(imported.id)

    assert [model.id for model in catalog.list_models()] == [
        "builtin/okay_nabu",
        "builtin/hey_jarvis",
        "builtin/hey_mycroft",
        "builtin/hey_rhasspy",
        "builtin/alexa",
    ]
    assert not Path(imported.target).exists()
    with pytest.raises(WakewordModelError, match="Built-in"):
        catalog.delete_model(DEFAULT_WAKEWORD_MODEL_ID)


def test_engine_uses_the_single_models_actual_prediction_labels() -> None:
    model = Mock()
    model.predict.return_value = {"custom_filename_stem": 0.73}
    engine = OpenWakeWordEngine(model_target=r"C:\models\custom.onnx")
    engine._model = model

    assert engine.detect(b"\0" * 2560) == pytest.approx(0.73)


def test_engine_clamps_sensitivity_away_from_always_triggering_threshold() -> None:
    most_sensitive = OpenWakeWordEngine(sensitivity=1.0)
    least_sensitive = OpenWakeWordEngine(sensitivity=0.0)

    assert most_sensitive.threshold == pytest.approx(1.0 - MAX_WAKEWORD_SENSITIVITY)
    assert least_sensitive.threshold == pytest.approx(1.0 - MIN_WAKEWORD_SENSITIVITY)


def test_engine_starts_the_resolved_model_target(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_model = Mock()
    create_model = Mock(return_value=loaded_model)
    monkeypatch.setattr(engine_module, "_create_openwakeword_model", create_model)
    engine = OpenWakeWordEngine(model_target="C:/models/computer.onnx")

    engine.start()

    create_model.assert_called_once_with("C:/models/computer.onnx", vad_threshold=0.3)
    assert engine._model is loaded_model


def test_catalog_creates_nabu_with_the_home_assistant_backend(tmp_path: Path) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    engine = catalog.create_engine("builtin/okay_nabu", sensitivity=0.7)

    assert isinstance(engine, PyOpenWakeWordEngine)
    assert engine._model_target == "okay_nabu"
    assert engine.threshold == pytest.approx(0.3)


def test_pyopen_engine_returns_strongest_streaming_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = Mock()
    model.process_streaming.side_effect = ([0.2, 0.8], [0.4])
    features = Mock()
    features.process_streaming.return_value = ["features-1", "features-2"]
    monkeypatch.setattr(
        engine_module,
        "_create_pyopenwakeword_components",
        Mock(return_value=(model, features)),
    )
    engine = PyOpenWakeWordEngine(model_target="okay_nabu")
    engine.start()

    score = engine.detect(b"\0" * 2560)
    engine.stop()

    assert score == pytest.approx(0.8)
    model.close.assert_called_once_with()
    features.close.assert_called_once_with()


def test_pyopen_engine_does_not_detect_without_a_positive_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = Mock()
    features = Mock()
    features.process_streaming.return_value = []
    monkeypatch.setattr(
        engine_module,
        "_create_pyopenwakeword_components",
        Mock(return_value=(model, features)),
    )
    engine = PyOpenWakeWordEngine(model_target="okay_nabu")
    engine.start()

    assert engine.detect(b"\0" * 2560) == 0.0
