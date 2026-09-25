"""Tests for the Desktop TFLite wakeword catalog and multi-model engine."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from desktop.wakeword import engine as engine_module
from desktop.wakeword.config import DEFAULT_MODEL_IDS, MAX_ACTIVE_PHRASES, PhraseConfig
from desktop.wakeword.engine import (
    DETECTOR_KIND_TFLITE_HEAD,
    MAX_CUSTOM_WAKEWORD_MODEL_BYTES,
    MultiWakewordEngine,
    WakewordMatch,
    WakewordModelCatalog,
    WakewordModelDescriptor,
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
    assert catalog.resolve(DEFAULT_MODEL_IDS[0]).builtin is True
    assert catalog.resolve(DEFAULT_MODEL_IDS[1]).target.endswith("hey_nabu_v2.tflite")


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
    ("filename", "content"),
    [
        ("model.onnx", b"data"),
        ("model.tflite", b""),
        ("", b"data"),
    ],
)
def test_catalog_rejects_invalid_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    content: bytes,
) -> None:
    monkeypatch.setattr(engine_module, "_validate_custom_model", Mock())
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    with pytest.raises(WakewordModelError):
        catalog.import_model(filename, content)


def test_catalog_rejects_oversized_import(tmp_path: Path) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    with pytest.raises(WakewordModelError):
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
    with pytest.raises(WakewordModelError) as rejected:
        catalog.delete_model(DEFAULT_MODEL_IDS[0])
    assert rejected.value.error_code == "wakeword_model_delete_failed"


def test_catalog_creates_two_model_engine_with_independent_thresholds(tmp_path: Path) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    engine = catalog.create_engine(
        [PhraseConfig("builtin/okay_nabu", 0.7), PhraseConfig("builtin/hey_nabu", 0.3)]
    )

    assert isinstance(engine, MultiWakewordEngine)
    assert engine.active_model_ids == DEFAULT_MODEL_IDS
    assert engine.thresholds == pytest.approx({"builtin/okay_nabu": 0.3, "builtin/hey_nabu": 0.7})


@pytest.mark.parametrize(
    "active_ids",
    [
        [],
        ["builtin/okay_nabu", "builtin/okay_nabu"],
        [*DEFAULT_MODEL_IDS, "builtin/unknown"],
        [f"custom/{index}" for index in range(MAX_ACTIVE_PHRASES + 1)],
    ],
)
def test_catalog_rejects_invalid_active_model_sets(tmp_path: Path, active_ids: list[str]) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    with pytest.raises(WakewordModelError):
        catalog.create_engine([PhraseConfig(model_id) for model_id in active_ids])


def test_catalog_rejects_more_than_the_phrase_limit_before_resolving(tmp_path: Path) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")
    phrases = [PhraseConfig(f"custom/{index}") for index in range(MAX_ACTIVE_PHRASES + 1)]

    with pytest.raises(WakewordModelError, match=f"between 1 and {MAX_ACTIVE_PHRASES}"):
        catalog.create_engine(phrases)


def test_catalog_rejects_entries_that_are_not_phrases(tmp_path: Path) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    with pytest.raises(WakewordModelError):
        catalog.create_engine([7])  # type: ignore[list-item]


def test_descriptors_report_detector_kind_and_overlapping_phrases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "_validate_custom_model", Mock())
    catalog = WakewordModelCatalog(tmp_path / "settings.json")
    imported = catalog.import_model("computer.tflite", b"tflite")

    models = {model.id: model.to_dict() for model in catalog.list_models()}

    assert all(model["kind"] == DETECTOR_KIND_TFLITE_HEAD for model in models.values())
    assert models["builtin/okay_nabu"]["overlaps"] == ["builtin/hey_nabu"]
    assert models["builtin/hey_nabu"]["overlaps"] == ["builtin/okay_nabu"]
    assert models["builtin/hey_jarvis"]["overlaps"] == []
    assert models[imported.id]["overlaps"] == []


def test_catalog_hosts_the_maximum_number_of_phrases_with_own_sensitivities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "_validate_custom_model", Mock())
    catalog = WakewordModelCatalog(tmp_path / "settings.json")
    imported = [catalog.import_model(f"phrase{index}.tflite", b"tflite") for index in range(2)]
    model_ids = [model.id for model in catalog.list_models()]
    assert len(model_ids) == MAX_ACTIVE_PHRASES
    phrases = [
        PhraseConfig(model_id, 0.1 * (index + 1)) for index, model_id in enumerate(model_ids)
    ]
    features = Mock()
    features.process_streaming.return_value = ["features"]
    models = {model_id: Mock() for model_id in model_ids}
    for model in models.values():
        model.process_streaming.return_value = [0.0]
    # A higher raw score (0.7 over threshold 0.4) loses to the better
    # score-to-threshold ratio (0.5 over threshold 0.2).
    models[model_ids[5]].process_streaming.return_value = [0.7]
    models[imported[1].id].process_streaming.return_value = [0.5]
    monkeypatch.setattr(
        engine_module, "_create_pyopenwakeword_features", Mock(return_value=features)
    )
    monkeypatch.setattr(
        engine_module,
        "_create_pyopenwakeword_model",
        lambda descriptor: models[descriptor.id],
    )

    engine = catalog.create_engine(phrases)
    engine.start()

    assert engine.active_model_ids == tuple(model_ids)
    assert engine.thresholds == pytest.approx(
        {phrase.model_id: 1.0 - phrase.sensitivity for phrase in phrases}
    )
    match = engine.detect(b"audio")
    assert match is not None
    assert (match.model_id, match.score, match.threshold) == (
        imported[1].id,
        0.5,
        pytest.approx(0.2),
    )
    engine.stop()
    assert all(model.close.call_count == 1 for model in models.values())


def _two_phrase_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_ids: tuple[str, str],
    scores: tuple[list[float], list[float]],
) -> MultiWakewordEngine:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")
    features = Mock()
    features.process_streaming.return_value = ["features"]
    models = []
    for model_scores in scores:
        model = Mock()
        model.process_streaming.side_effect = [[score] for score in model_scores]
        models.append(model)
    monkeypatch.setattr(
        engine_module, "_create_pyopenwakeword_features", Mock(return_value=features)
    )
    monkeypatch.setattr(engine_module, "_create_pyopenwakeword_model", Mock(side_effect=models))
    engine = catalog.create_engine([PhraseConfig(model_id) for model_id in model_ids])
    engine.start()
    return engine


def test_engine_rearms_each_phrase_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jarvis, alexa = "builtin/hey_jarvis", "builtin/alexa"
    engine = _two_phrase_engine(
        tmp_path,
        monkeypatch,
        (jarvis, alexa),
        ([0.9, 0.9, 0.9, 0.1, 0.9], [0.1, 0.9, 0.9, 0.9, 0.9]),
    )

    # jarvis fires; alexa still fires while jarvis stays above its threshold.
    assert engine.detect(b"1") == WakewordMatch(jarvis, 0.9, 0.5)
    assert engine.detect(b"2") == WakewordMatch(alexa, 0.9, 0.5)
    # Neither re-fires until its own score drops below its threshold.
    assert engine.detect(b"3") is None
    assert engine.detect(b"4") is None
    assert engine.detect(b"5") == WakewordMatch(jarvis, 0.9, 0.5)


def test_engine_rearms_overlapping_phrases_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    okay, hey = DEFAULT_MODEL_IDS
    engine = _two_phrase_engine(
        tmp_path,
        monkeypatch,
        (okay, hey),
        ([0.1, 0.9, 0.9, 0.1, 0.1, 0.9], [0.9, 0.1, 0.1, 0.1, 0.1, 0.1]),
    )

    # One utterance: hey peaks first, then okay rises before both are quiet.
    assert engine.detect(b"1") == WakewordMatch(hey, 0.9, 0.5)
    assert engine.detect(b"2") is None
    assert engine.detect(b"3") is None
    # A window with both below their thresholds re-arms the pair.
    assert engine.detect(b"4") is None
    assert engine.detect(b"5") is None
    assert engine.detect(b"6") == WakewordMatch(okay, 0.9, 0.5)


def test_engine_rejects_an_unsupported_detector_kind_and_releases_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    features = Mock()
    monkeypatch.setattr(
        engine_module, "_create_pyopenwakeword_features", Mock(return_value=features)
    )
    descriptor = WakewordModelDescriptor(
        id="custom/verifier",
        label="Verifier",
        source="imported",
        format="onnx",
        removable=True,
        target="verifier.onnx",
        kind="speaker_verifier",
    )
    engine = MultiWakewordEngine((descriptor,), {})

    with pytest.raises(WakewordModelError) as raised:
        engine.start()

    assert raised.value.error_code == "wakeword_model_unavailable"
    features.close.assert_called_once_with()
    assert engine.detect(b"audio") is None


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
        [PhraseConfig("builtin/okay_nabu", 0.5), PhraseConfig("builtin/hey_nabu", 0.75)],
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
    engine = catalog.create_engine([PhraseConfig(model_id) for model_id in DEFAULT_MODEL_IDS])
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
    engine = catalog.create_engine([PhraseConfig(DEFAULT_MODEL_IDS[0])])

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
    engine = catalog.create_engine([PhraseConfig(DEFAULT_MODEL_IDS[0])])
    engine.start()

    assert engine.detect(b"first") is not None
    assert engine.detect(b"still-high") is None
    assert engine.detect(b"low") is None
    assert engine.detect(b"second") is not None


def _scripted_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scores: list[list[float]],
    *,
    score_listener=None,
):
    catalog = WakewordModelCatalog(tmp_path / "settings.json")
    features = Mock()
    features.process_streaming.return_value = ["features"]
    model = Mock()
    model.process_streaming.side_effect = scores
    monkeypatch.setattr(
        engine_module, "_create_pyopenwakeword_features", Mock(return_value=features)
    )
    monkeypatch.setattr(engine_module, "_create_pyopenwakeword_model", Mock(return_value=model))
    engine = catalog.create_engine(
        [PhraseConfig(DEFAULT_MODEL_IDS[0])], score_listener=score_listener
    )
    engine.start()
    return engine


def test_engine_detects_a_confident_single_window_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _scripted_engine(tmp_path, monkeypatch, [[0.68]])

    assert engine.detect(b"audio") == WakewordMatch(DEFAULT_MODEL_IDS[0], 0.68, 0.5)


def test_engine_filters_an_isolated_marginal_score_spike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _scripted_engine(tmp_path, monkeypatch, [[0.55], [0.0], [0.0], [0.0], [0.0], [0.0]])

    for _ in range(6):
        assert engine.detect(b"audio") is None


def test_engine_confirms_a_repeated_marginal_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _scripted_engine(tmp_path, monkeypatch, [[0.55], [0.0], [0.55]])

    assert engine.detect(b"audio") is None
    assert engine.detect(b"audio") is None
    assert engine.detect(b"audio") == WakewordMatch(DEFAULT_MODEL_IDS[0], 0.55, 0.5)


def test_engine_ignores_marginal_scores_beyond_the_confirmation_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _scripted_engine(tmp_path, monkeypatch, [[0.55], [0.0], [0.0], [0.0], [0.0], [0.55]])

    for _ in range(5):
        engine.detect(b"audio")
    assert engine.detect(b"audio") is None


def test_engine_zeroes_scores_when_no_speech_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_scores: list[dict[str, float]] = []
    engine = _scripted_engine(
        tmp_path,
        monkeypatch,
        [[0.9], [0.9]],
        score_listener=observed_scores.append,
    )

    assert engine.detect(b"audio", speech_present=False) is None
    assert engine.detect(b"audio", speech_present=False) is None

    assert observed_scores == [{DEFAULT_MODEL_IDS[0]: 0.0}] * 2


def test_engine_does_not_count_gated_chunks_toward_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _scripted_engine(tmp_path, monkeypatch, [[0.55], [0.9], [0.55]])

    assert engine.detect(b"audio") is None
    assert engine.detect(b"audio", speech_present=False) is None
    assert engine.detect(b"audio") is not None


def test_engine_rearms_on_raw_scores_so_a_gated_pause_cannot_fire_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One utterance whose score stays high across a short pause that closes the
    # speech gate, then a second utterance after the score dropped (gated too).
    raw_scores = [0.9, 0.9, 0.9, 0.9, 0.1, 0.9]
    speech = [True, True, False, True, False, True]
    observed_scores: list[dict[str, float]] = []
    engine = _scripted_engine(
        tmp_path,
        monkeypatch,
        [[score] for score in raw_scores],
        score_listener=observed_scores.append,
    )

    matches = [engine.detect(b"audio", speech_present=present) for present in speech]

    match = WakewordMatch(DEFAULT_MODEL_IDS[0], 0.9, 0.5)
    assert matches == [match, None, None, None, None, match]
    assert [scores[DEFAULT_MODEL_IDS[0]] for scores in observed_scores] == [
        0.9,
        0.9,
        0.0,
        0.9,
        0.0,
        0.9,
    ]


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
