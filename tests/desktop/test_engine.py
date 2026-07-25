"""Tests for the Desktop TFLite wakeword catalog and multi-model engine."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from desktop.wakeword import engine as engine_module
from desktop.wakeword.engine import (
    DEFAULT_WAKEWORD_MODEL_IDS,
    MAX_CUSTOM_WAKEWORD_MODEL_BYTES,
    MultiWakewordEngine,
    WakewordMatch,
    WakewordModelCatalog,
    WakewordModelError,
)


def test_catalog_lists_all_tflite_builtin_models_with_nabu_first(tmp_path: Path) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    models = [model.to_dict() for model in catalog.list_models()]

    assert [model["id"] for model in models] == [
        "builtin/okay_nabu",
        "builtin/hey_nabu",
        "builtin/hey_jarvis",
        "builtin/hey_mycroft",
        "builtin/hey_rhasspy",
        "builtin/alexa",
    ]
    assert models[0]["label"] == "Okay Nabu"
    assert models[1]["label"] == "Hey Nabu"
    assert all(model["source"] == "built_in" for model in models)
    assert all(model["format"] == "tflite" for model in models)
    assert all(model["removable"] is False for model in models)
    assert all("target" not in model for model in models)
    assert catalog.resolve(DEFAULT_WAKEWORD_MODEL_IDS[0]).builtin is True
    assert catalog.resolve(DEFAULT_WAKEWORD_MODEL_IDS[1]).target.endswith("hey_nabu_v2.tflite")


def test_catalog_imports_and_resolves_a_valid_custom_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "_validate_custom_model", Mock())
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    imported = catalog.import_model(r"C:\downloads\hey_computer-v1.tflite", b"tflite-bytes")

    assert imported.id.startswith("custom/")
    assert imported.label == "hey computer v1"
    assert imported.format == "tflite"
    assert imported.removable is True
    assert Path(imported.target).read_bytes() == b"tflite-bytes"
    assert catalog.resolve(imported.id) == imported
    metadata = json.loads(Path(imported.target).with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["id"] == imported.id
    assert metadata["filename"] == Path(imported.target).name
    assert metadata["format"] == "tflite"


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("model.onnx", b"data", "TFLite"),
        ("model.tflite", b"", "empty"),
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
        catalog.import_model("large.tflite", b"x" * (MAX_CUSTOM_WAKEWORD_MODEL_BYTES + 1))


def test_catalog_cleans_up_a_model_that_fails_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    def reject(_path: Path) -> None:
        raise WakewordModelError("bad model")

    monkeypatch.setattr(engine_module, "_validate_custom_model", reject)

    with pytest.raises(WakewordModelError, match="bad model"):
        catalog.import_model("bad.tflite", b"not-tflite")

    assert list((tmp_path / "wakewords").iterdir()) == []


def test_catalog_ignores_obsolete_or_tampered_custom_metadata(tmp_path: Path) -> None:
    model_dir = tmp_path / "wakewords"
    model_dir.mkdir()
    token = "a" * 32
    (model_dir / f"{token}.tflite").write_bytes(b"model")
    (model_dir / f"{token}.json").write_text(
        json.dumps(
            {
                "id": f"custom/{token}",
                "label": "Legacy",
                "filename": f"{token}.tflite",
                "format": "onnx",
            }
        ),
        encoding="utf-8",
    )

    assert len(WakewordModelCatalog(tmp_path / "settings.json").list_models()) == 6


def test_catalog_deletes_only_imported_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "_validate_custom_model", Mock())
    catalog = WakewordModelCatalog(tmp_path / "settings.json")
    imported = catalog.import_model("computer.tflite", b"tflite")

    catalog.delete_model(imported.id)

    assert [model.id for model in catalog.list_models()] == [
        "builtin/okay_nabu",
        "builtin/hey_nabu",
        "builtin/hey_jarvis",
        "builtin/hey_mycroft",
        "builtin/hey_rhasspy",
        "builtin/alexa",
    ]
    assert not Path(imported.target).exists()
    with pytest.raises(WakewordModelError, match="Built-in"):
        catalog.delete_model(DEFAULT_WAKEWORD_MODEL_IDS[0])


def test_catalog_creates_two_model_engine_with_independent_thresholds(tmp_path: Path) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    engine = catalog.create_engine(
        list(DEFAULT_WAKEWORD_MODEL_IDS),
        {
            "builtin/okay_nabu": 0.7,
            "builtin/hey_nabu": 0.3,
        },
    )

    assert isinstance(engine, MultiWakewordEngine)
    assert engine.active_model_ids == DEFAULT_WAKEWORD_MODEL_IDS
    assert engine.thresholds == pytest.approx({"builtin/okay_nabu": 0.3, "builtin/hey_nabu": 0.7})


@pytest.mark.parametrize(
    "active_ids",
    [[], ["builtin/okay_nabu", "builtin/okay_nabu"], [*DEFAULT_WAKEWORD_MODEL_IDS, "x"]],
)
def test_catalog_rejects_invalid_active_model_sets(tmp_path: Path, active_ids: list[str]) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    with pytest.raises(WakewordModelError):
        catalog.create_engine(active_ids)


def test_engine_shares_features_and_selects_threshold_normalized_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")
    features = Mock()
    features.process_streaming.return_value = ["features-1", "features-2"]
    okay_model = Mock()
    okay_model.process_streaming.side_effect = ([0.2], [0.8])
    hey_model = Mock()
    hey_model.process_streaming.side_effect = ([0.5], [0.1])
    create_features = Mock(return_value=features)
    create_model = Mock(side_effect=[okay_model, hey_model])
    observed_scores: list[dict[str, float]] = []
    monkeypatch.setattr(engine_module, "_create_pyopenwakeword_features", create_features)
    monkeypatch.setattr(engine_module, "_create_pyopenwakeword_model", create_model)
    engine = catalog.create_engine(
        list(DEFAULT_WAKEWORD_MODEL_IDS),
        {"builtin/okay_nabu": 0.5, "builtin/hey_nabu": 0.75},
        score_listener=observed_scores.append,
    )

    engine.start()
    match = engine.detect(b"\0" * 2560)
    engine.stop()

    assert match == WakewordMatch("builtin/hey_nabu", 0.5, 0.25)
    assert observed_scores == [
        {
            "builtin/okay_nabu": 0.8,
            "builtin/hey_nabu": 0.5,
        }
    ]
    create_features.assert_called_once_with()
    assert create_model.call_count == 2
    features.process_streaming.assert_called_once_with(b"\0" * 2560)
    assert [call.args[0] for call in okay_model.process_streaming.call_args_list] == [
        "features-1",
        "features-2",
    ]
    assert [call.args[0] for call in hey_model.process_streaming.call_args_list] == [
        "features-1",
        "features-2",
    ]
    okay_model.close.assert_called_once_with()
    hey_model.close.assert_called_once_with()
    features.close.assert_called_once_with()


def test_engine_returns_one_stable_match_when_models_tie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")
    features = Mock()
    features.process_streaming.return_value = ["features"]
    okay_model = Mock()
    okay_model.process_streaming.return_value = [0.75]
    hey_model = Mock()
    hey_model.process_streaming.return_value = [0.75]
    monkeypatch.setattr(
        engine_module, "_create_pyopenwakeword_features", Mock(return_value=features)
    )
    monkeypatch.setattr(
        engine_module,
        "_create_pyopenwakeword_model",
        Mock(side_effect=[okay_model, hey_model]),
    )
    engine = catalog.create_engine(list(DEFAULT_WAKEWORD_MODEL_IDS))
    engine.start()

    assert engine.detect(b"audio") == WakewordMatch("builtin/okay_nabu", 0.75, 0.5)


def test_engine_returns_no_match_without_features_or_threshold_crossing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")
    features = Mock()
    features.process_streaming.return_value = []
    model = Mock()
    monkeypatch.setattr(
        engine_module, "_create_pyopenwakeword_features", Mock(return_value=features)
    )
    monkeypatch.setattr(engine_module, "_create_pyopenwakeword_model", Mock(return_value=model))
    engine = catalog.create_engine([DEFAULT_WAKEWORD_MODEL_IDS[0]])

    assert engine.detect(b"audio") is None
    engine.start()
    assert engine.detect(b"audio") is None


def test_engine_rearms_only_after_all_scores_drop_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")
    features = Mock()
    features.process_streaming.return_value = ["features"]
    model = Mock()
    model.process_streaming.side_effect = ([0.8], [0.9], [0.1], [0.7])
    monkeypatch.setattr(
        engine_module, "_create_pyopenwakeword_features", Mock(return_value=features)
    )
    monkeypatch.setattr(engine_module, "_create_pyopenwakeword_model", Mock(return_value=model))
    engine = catalog.create_engine([DEFAULT_WAKEWORD_MODEL_IDS[0]])
    engine.start()

    assert engine.detect(b"first") is not None
    assert engine.detect(b"still-high") is None
    assert engine.detect(b"low") is None
    assert engine.detect(b"second") is not None


def test_custom_model_validation_closes_a_loadable_detector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = Mock()
    create_model = Mock(return_value=model)
    monkeypatch.setattr(engine_module, "_create_pyopenwakeword_model", create_model)
    path = tmp_path / "custom.tflite"

    engine_module._validate_custom_model(path)

    descriptor = create_model.call_args.args[0]
    assert descriptor.target == str(path)
    assert descriptor.builtin is False
    model.close.assert_called_once_with()


def test_bundled_hey_nabu_model_has_pinned_checksum() -> None:
    model_path = Path(engine_module._BUNDLED_HEY_NABU_PATH)

    assert model_path.is_file()
    assert hashlib.sha256(model_path.read_bytes()).hexdigest() == (
        "ce18b69e1bddfb56e70fe739d6ca0f423f70a6e710f05b376baf6a3625689234"
    )
