"""The wake phrase model catalog and the multi-phrase engine.

Scripted tests replace the openWakeWord feature stream and phrase heads
through the engine's model factories; the real-audio tests at the end run the
bundled models on recorded phrases.
"""

from __future__ import annotations

import hashlib
import json
import wave
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from desktop.wakeword import engine as engine_module
from desktop.wakeword._speech_detection import SpeechDetector, SpeechGate
from desktop.wakeword.config import DEFAULT_MODEL_IDS, MAX_ACTIVE_PHRASES, PhraseConfig
from desktop.wakeword.engine import (
    DETECTOR_KIND_TFLITE_HEAD,
    MAX_CUSTOM_WAKEWORD_MODEL_BYTES,
    MockWakewordEngine,
    MultiWakewordEngine,
    WakewordEngine,
    WakewordMatch,
    WakewordModelCatalog,
    WakewordModelDescriptor,
    WakewordModelError,
)

BUILTIN_IDS = [
    "builtin/okay_nabu",
    "builtin/hey_nabu",
    "builtin/hey_jarvis",
    "builtin/hey_mycroft",
    "builtin/hey_rhasspy",
    "builtin/alexa",
]
OKAY, HEY = DEFAULT_MODEL_IDS
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "wakeword"


class ScriptedFeatures:
    """Feature stream double: every chunk yields ``frames`` feature frames."""

    def __init__(self, frames: int = 1) -> None:
        self.frames = frames
        self.chunks: list[bytes] = []
        self.closed = False

    def process(self, chunk: bytes) -> list[str]:
        self.chunks.append(chunk)
        return [f"features-{len(self.chunks)}-{frame}" for frame in range(self.frames)]

    def close(self) -> None:
        self.closed = True


class ScriptedHead:
    """Phrase head double answering each feature frame with the next scripted score."""

    def __init__(self, scores: Iterable[float] = (), *, constant: float | None = None) -> None:
        self._scores = iter(scores)
        self._constant = constant
        self.frames: list[str] = []
        self.closed = False

    def scores(self, frames: list[str]) -> list[float]:
        self.frames.extend(frames)
        return [
            self._constant if self._constant is not None else next(self._scores) for _ in frames
        ]

    def close(self) -> None:
        self.closed = True


class ScriptedModels:
    """Installs scripted features and heads; heads are keyed by model id."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.features = ScriptedFeatures()
        self.heads: dict[str, ScriptedHead] = {}
        self.created: list[WakewordModelDescriptor] = []
        monkeypatch.setattr(engine_module, "_create_feature_stream", self._features)
        monkeypatch.setattr(engine_module, "_create_phrase_head", self._head)

    def _features(self) -> ScriptedFeatures:
        return self.features

    def _head(self, descriptor: WakewordModelDescriptor) -> ScriptedHead:
        self.created.append(descriptor)
        return self.heads.setdefault(descriptor.id, ScriptedHead(constant=0.0))


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> ScriptedModels:
    return ScriptedModels(monkeypatch)


@pytest.fixture
def catalog(tmp_path: Path) -> WakewordModelCatalog:
    return WakewordModelCatalog(tmp_path / "settings.json")


def _started(
    catalog: WakewordModelCatalog,
    phrases: list[PhraseConfig],
    score_listener: Callable[[dict[str, float]], None] | None = None,
) -> MultiWakewordEngine:
    engine = catalog.create_engine(phrases, score_listener=score_listener)
    engine.start()
    return engine


# -- Catalog -------------------------------------------------------------------------


def test_catalog_lists_the_builtin_models_with_nabu_first(catalog: WakewordModelCatalog) -> None:
    models = [model.to_dict() for model in catalog.list_models()]

    assert [model["id"] for model in models] == BUILTIN_IDS
    assert [models[0]["label"], models[1]["label"]] == ["Okay Nabu", "Hey Nabu"]
    for model in models:
        assert (model["source"], model["format"], model["removable"]) == (
            "built_in",
            "tflite",
            False,
        )
        assert model["kind"] == DETECTOR_KIND_TFLITE_HEAD
        assert "target" not in model
    overlaps = {model["id"]: model["overlaps"] for model in models}
    assert overlaps[OKAY] == [HEY]
    assert overlaps[HEY] == [OKAY]
    assert overlaps["builtin/hey_jarvis"] == []
    assert catalog.resolve(OKAY).builtin is True


def test_bundled_hey_nabu_model_has_pinned_checksum(catalog: WakewordModelCatalog) -> None:
    model_path = Path(catalog.resolve(HEY).target)

    assert model_path.name == "hey_nabu_v2.tflite"
    assert hashlib.sha256(model_path.read_bytes()).hexdigest() == (
        "ce18b69e1bddfb56e70fe739d6ca0f423f70a6e710f05b376baf6a3625689234"
    )


def test_catalog_imports_validates_and_resolves_a_custom_model(
    catalog: WakewordModelCatalog, models: ScriptedModels
) -> None:
    imported = catalog.import_model(r"C:\downloads\hey_computer-v1.tflite", b"tflite-bytes")

    assert imported.id.startswith("custom/")
    assert (imported.label, imported.format, imported.removable) == (
        "hey computer v1",
        "tflite",
        True,
    )
    assert Path(imported.target).read_bytes() == b"tflite-bytes"
    assert catalog.resolve(imported.id) == imported
    metadata = json.loads(Path(imported.target).with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["id"] == imported.id
    assert metadata["filename"] == Path(imported.target).name
    assert metadata["format"] == "tflite"
    # The import loaded the file once as a detector before storing it, then released it.
    [validated] = models.created
    assert validated.builtin is False
    assert Path(validated.target).parent == Path(imported.target).parent
    assert models.heads[validated.id].closed
    listed = {model.id: model.to_dict() for model in catalog.list_models()}
    assert listed[imported.id]["kind"] == DETECTOR_KIND_TFLITE_HEAD
    assert listed[imported.id]["overlaps"] == []


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("model.onnx", b"data"),
        ("model.tflite", b""),
        ("", b"data"),
        ("large.tflite", b"x" * (MAX_CUSTOM_WAKEWORD_MODEL_BYTES + 1)),
    ],
    ids=["not-tflite", "empty", "no-name", "oversized"],
)
def test_catalog_rejects_invalid_imports(
    catalog: WakewordModelCatalog, models: ScriptedModels, filename: str, content: bytes
) -> None:
    with pytest.raises(WakewordModelError):
        catalog.import_model(filename, content)

    assert models.created == []


def test_catalog_cleans_up_a_model_that_fails_validation(
    tmp_path: Path, catalog: WakewordModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(_descriptor: WakewordModelDescriptor) -> None:
        raise WakewordModelError("bad model")

    monkeypatch.setattr(engine_module, "_create_phrase_head", reject)

    with pytest.raises(WakewordModelError, match="not a compatible TFLite wakeword model"):
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

    assert [
        model.id for model in WakewordModelCatalog(tmp_path / "settings.json").list_models()
    ] == (BUILTIN_IDS)


def test_catalog_deletes_only_imported_models(
    catalog: WakewordModelCatalog, models: ScriptedModels
) -> None:
    imported = catalog.import_model("computer.tflite", b"tflite")

    catalog.delete_model(imported.id)

    assert [model.id for model in catalog.list_models()] == BUILTIN_IDS
    assert not Path(imported.target).exists()
    with pytest.raises(WakewordModelError) as rejected:
        catalog.delete_model(OKAY)
    assert rejected.value.error_code == "wakeword_model_delete_failed"


@pytest.mark.parametrize(
    ("phrases", "message"),
    [
        ([], None),
        ([PhraseConfig(OKAY), PhraseConfig(OKAY)], None),
        ([PhraseConfig(OKAY), PhraseConfig(HEY), PhraseConfig("builtin/unknown")], None),
        (
            [PhraseConfig(f"custom/{index}") for index in range(MAX_ACTIVE_PHRASES + 1)],
            f"between 1 and {MAX_ACTIVE_PHRASES}",
        ),
        ([7], None),
    ],
    ids=["none", "duplicate", "unknown", "over-the-limit-before-resolving", "not-a-phrase"],
)
def test_catalog_rejects_invalid_active_phrase_sets(
    catalog: WakewordModelCatalog, phrases: list[Any], message: str | None
) -> None:
    with pytest.raises(WakewordModelError, match=message):
        catalog.create_engine(phrases)


def test_catalog_hosts_the_maximum_number_of_phrases_with_own_sensitivities(
    catalog: WakewordModelCatalog, models: ScriptedModels
) -> None:
    imported = [catalog.import_model(f"phrase{index}.tflite", b"tflite") for index in range(2)]
    model_ids = [model.id for model in catalog.list_models()]
    assert len(model_ids) == MAX_ACTIVE_PHRASES
    phrases = [
        PhraseConfig(model_id, 0.1 * (index + 1)) for index, model_id in enumerate(model_ids)
    ]
    # A higher raw score (0.7 over threshold 0.4) loses to the better
    # score-to-threshold ratio (0.5 over threshold 0.2).
    models.heads.clear()
    models.heads[model_ids[5]] = ScriptedHead(constant=0.7)
    models.heads[imported[1].id] = ScriptedHead(constant=0.5)

    engine = _started(catalog, phrases)

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
    assert all(models.heads[model_id].closed for model_id in model_ids)
    assert models.features.closed


# -- Engine scoring --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("okay_scores", "hey_scores", "sensitivities", "match"),
    [
        ([0.2, 0.8], [0.5, 0.1], (0.5, 0.75), WakewordMatch(HEY, 0.5, 0.25)),
        ([0.75, 0.75], [0.75, 0.75], (0.5, 0.5), WakewordMatch(OKAY, 0.75, 0.5)),
        ([0.3, 0.4], [0.1, 0.2], (0.5, 0.5), None),
    ],
    ids=["threshold-normalized-winner", "tie-keeps-phrase-order", "no-crossing"],
)
def test_engine_scores_shared_features_and_reports_the_best_phrase(
    catalog: WakewordModelCatalog,
    models: ScriptedModels,
    okay_scores: list[float],
    hey_scores: list[float],
    sensitivities: tuple[float, float],
    match: WakewordMatch | None,
) -> None:
    models.features.frames = 2
    models.heads[OKAY] = ScriptedHead(okay_scores)
    models.heads[HEY] = ScriptedHead(hey_scores)
    observed: list[dict[str, float]] = []
    engine = _started(
        catalog,
        [PhraseConfig(OKAY, sensitivities[0]), PhraseConfig(HEY, sensitivities[1])],
        score_listener=observed.append,
    )

    assert engine.detect(b"\0" * 2560) == match
    engine.stop()

    # One feature pass per chunk feeds every head; a chunk scores its best frame.
    assert models.features.chunks == [b"\0" * 2560]
    assert models.heads[OKAY].frames == models.heads[HEY].frames == ["features-1-0", "features-1-1"]
    assert observed == [{OKAY: max(okay_scores), HEY: max(hey_scores)}]
    assert models.heads[OKAY].closed and models.heads[HEY].closed and models.features.closed


def test_engine_matches_nothing_before_start_or_without_features(
    catalog: WakewordModelCatalog, models: ScriptedModels
) -> None:
    models.features.frames = 0
    engine = catalog.create_engine([PhraseConfig(OKAY)])

    assert engine.detect(b"audio") is None
    engine.start()
    assert engine.detect(b"audio") is None


def test_engine_rejects_an_unsupported_detector_kind_and_releases_features(
    models: ScriptedModels,
) -> None:
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
    assert models.features.closed
    assert engine.detect(b"audio") is None


# Each case scripts one raw score per chunk and whether the speech gate admits it.
# A match reports the score; the score listener always sees gated scores.
@pytest.mark.parametrize(
    ("scores", "speech", "matches"),
    [
        ([0.68], [True], [0.68]),
        ([0.55, 0.0, 0.0, 0.0, 0.0, 0.0], [True] * 6, [None] * 6),
        ([0.55, 0.0, 0.55], [True] * 3, [None, None, 0.55]),
        ([0.55, 0.0, 0.0, 0.0, 0.0, 0.55], [True] * 6, [None] * 6),
        ([0.9, 0.9], [False, False], [None, None]),
        ([0.55, 0.9, 0.55], [True, False, True], [None, None, 0.55]),
        ([0.8, 0.9, 0.1, 0.7], [True] * 4, [0.8, None, None, 0.7]),
        (
            [0.9, 0.9, 0.9, 0.9, 0.1, 0.9],
            [True, True, False, True, False, True],
            [0.9, None, None, None, None, 0.9],
        ),
    ],
    ids=[
        "confident-single-window",
        "isolated-marginal-spike",
        "repeated-marginal-score",
        "marginal-beyond-confirmation-window",
        "no-speech-zeroes-scores",
        "gated-chunks-do-not-confirm",
        "rearms-after-the-score-drops",
        "rearms-on-raw-scores-across-a-gated-pause",
    ],
)
def test_engine_confirms_marginal_scores_and_rearms_after_a_phrase(
    catalog: WakewordModelCatalog,
    models: ScriptedModels,
    scores: list[float],
    speech: list[bool],
    matches: list[float | None],
) -> None:
    models.heads[OKAY] = ScriptedHead(scores)
    observed: list[dict[str, float]] = []
    engine = _started(catalog, [PhraseConfig(OKAY)], score_listener=observed.append)

    results = [engine.detect(b"audio", speech_present=present) for present in speech]

    assert results == [
        None if score is None else WakewordMatch(OKAY, score, 0.5) for score in matches
    ]
    assert [frame[OKAY] for frame in observed] == [
        score if present else 0.0 for score, present in zip(scores, speech, strict=True)
    ]


def test_engine_rearms_each_phrase_independently(
    catalog: WakewordModelCatalog, models: ScriptedModels
) -> None:
    jarvis, alexa = "builtin/hey_jarvis", "builtin/alexa"
    models.heads[jarvis] = ScriptedHead([0.9, 0.9, 0.9, 0.1, 0.9])
    models.heads[alexa] = ScriptedHead([0.1, 0.9, 0.9, 0.9, 0.9])
    engine = _started(catalog, [PhraseConfig(jarvis), PhraseConfig(alexa)])

    # jarvis fires; alexa still fires while jarvis stays above its threshold.
    # Neither re-fires until its own score drops below its threshold.
    assert [engine.detect(b"audio") for _ in range(5)] == [
        WakewordMatch(jarvis, 0.9, 0.5),
        WakewordMatch(alexa, 0.9, 0.5),
        None,
        None,
        WakewordMatch(jarvis, 0.9, 0.5),
    ]


def test_engine_rearms_overlapping_phrases_together(
    catalog: WakewordModelCatalog, models: ScriptedModels
) -> None:
    models.heads[OKAY] = ScriptedHead([0.1, 0.9, 0.9, 0.1, 0.1, 0.9])
    models.heads[HEY] = ScriptedHead([0.9, 0.1, 0.1, 0.1, 0.1, 0.1])
    engine = _started(catalog, [PhraseConfig(OKAY), PhraseConfig(HEY)])

    # One utterance: hey peaks first, then okay rises before both are quiet.
    # A window with both below their thresholds re-arms the pair.
    assert [engine.detect(b"audio") for _ in range(6)] == [
        WakewordMatch(HEY, 0.9, 0.5),
        None,
        None,
        None,
        None,
        WakewordMatch(OKAY, 0.9, 0.5),
    ]


def test_mock_engine_cycles_its_scores_between_start_and_stop() -> None:
    engine = MockWakewordEngine(score_sequence=[0.2, 0.5, 0.9], model_id=HEY)
    assert engine.detect(b"audio") is None  # not started

    engine.start()
    assert [engine.detect(b"audio") for _ in range(4)] == [
        None,
        WakewordMatch(HEY, 0.5, 0.5),
        WakewordMatch(HEY, 0.9, 0.5),
        None,
    ]

    engine.set_score_sequence([0.1, 1.0])
    assert [engine.detect(b"audio") for _ in range(2)] == [None, WakewordMatch(HEY, 1.0, 0.5)]
    engine.stop()
    assert engine.detect(b"audio") is None


# -- Bundled models on real audio ----------------------------------------------------

_CHUNK_BYTES = 1280 * 2
_SAMPLE_RATE = 16000


@pytest.fixture(scope="module")
def real_models() -> Iterator[None]:
    pytest.importorskip("pyopen_wakeword")
    yield


def _speech_gate() -> SpeechGate:
    pytest.importorskip("onnxruntime")
    detector = SpeechDetector.create()
    assert detector is not None
    return SpeechGate(detector)


def _read_pcm16_mono(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        assert wav_file.getsampwidth() == 2
        samples = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if sample_rate != _SAMPLE_RATE:
        source_positions = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
        target_length = round(len(samples) * _SAMPLE_RATE / sample_rate)
        target_positions = np.linspace(0.0, 1.0, num=target_length, endpoint=False)
        samples = np.interp(target_positions, source_positions, samples).astype(np.int16)
    return samples.tobytes()


def _detect_file(engine: WakewordEngine, path: Path, gate: SpeechGate | None) -> list[str]:
    padding = np.zeros(_SAMPLE_RATE, dtype=np.int16).tobytes()
    stream = padding + _read_pcm16_mono(path) + padding
    matches: list[str] = []
    engine.start()
    try:
        for offset in range(0, len(stream), _CHUNK_BYTES):
            chunk = stream[offset : offset + _CHUNK_BYTES].ljust(_CHUNK_BYTES, b"\0")
            speech_present = gate.admits(chunk) if gate is not None else True
            match = engine.detect(chunk, speech_present=speech_present)
            if match is not None:
                matches.append(match.model_id)
    finally:
        engine.stop()
    return matches


# The heads score a phrase highest after it ended; the detection loop's delayed
# speech gate must still let that peak through. Without a speech detector the
# gate stays open, so two active models must also keep one activation per phrase
# on ungated audio.
@pytest.mark.parametrize(
    ("model_ids", "fixture_name", "gated", "expected"),
    [
        ([OKAY], "okay_nabu.wav", True, [OKAY]),
        ([HEY], "hey_nabu.wav", True, [HEY]),
        (DEFAULT_MODEL_IDS, "okay_nabu.wav", True, 1),
        (DEFAULT_MODEL_IDS, "hey_nabu.wav", True, 1),
        (DEFAULT_MODEL_IDS, "okay_nabu.wav", False, 1),
        (DEFAULT_MODEL_IDS, "hey_nabu.wav", False, 1),
        (DEFAULT_MODEL_IDS, "unrelated_hey_jarvis.wav", False, 0),
    ],
    ids=[
        "okay-gated",
        "hey-gated",
        "both-okay-gated",
        "both-hey-gated",
        "both-okay-open",
        "both-hey-open",
        "both-unrelated-open",
    ],
)
def test_bundled_nabu_models_activate_once_per_phrase_on_real_audio(
    real_models: None,
    model_ids: list[str],
    fixture_name: str,
    gated: bool,
    expected: list[str] | int,
) -> None:
    engine = WakewordModelCatalog(FIXTURES / "settings.json").create_engine(
        [PhraseConfig(model_id) for model_id in model_ids]
    )

    matches = _detect_file(engine, FIXTURES / fixture_name, _speech_gate() if gated else None)

    if isinstance(expected, int):
        assert len(matches) == expected
        assert set(matches) <= set(DEFAULT_MODEL_IDS)
    else:
        assert matches == expected


_PHRASE_FIXTURES = ("okay_nabu.wav", "hey_nabu.wav", "unrelated_hey_jarvis.wav")


def _reference_scores(fixture_name: str) -> list[dict[str, float]]:
    """Per-chunk scores of pyopen-wakeword's own streaming, which the thresholds were tuned on."""
    from pyopen_wakeword import Model, OpenWakeWord, OpenWakeWordFeatures

    features = OpenWakeWordFeatures.from_builtin()
    heads = {
        OKAY: OpenWakeWord.from_builtin(Model("okay_nabu")),
        HEY: OpenWakeWord.from_model(engine_module._BUNDLED_HEY_NABU_PATH),
    }
    scores: list[dict[str, float]] = []
    try:
        for chunk in _chunks(FIXTURES / fixture_name):
            batches = list(features.process_streaming(chunk))
            scores.append(
                {
                    model_id: max(
                        (
                            min(1.0, max(0.0, score))
                            for batch in batches
                            for score in head.process_streaming(batch)
                        ),
                        default=0.0,
                    )
                    for model_id, head in heads.items()
                }
            )
    finally:
        features.close()
        for head in heads.values():
            head.close()
    return scores


def _chunks(path: Path) -> list[bytes]:
    padding = np.zeros(_SAMPLE_RATE, dtype=np.int16).tobytes()
    stream = padding + _read_pcm16_mono(path) + padding
    return [
        stream[offset : offset + _CHUNK_BYTES].ljust(_CHUNK_BYTES, b"\0")
        for offset in range(0, len(stream), _CHUNK_BYTES)
    ]


@pytest.fixture(scope="module")
def reference_scores(real_models: None) -> dict[str, list[dict[str, float]]]:
    return {name: _reference_scores(name) for name in _PHRASE_FIXTURES}


# vBot runs openWakeWord on its own interpreters (one thread, XNNPACK when the
# library has it); every chunk must still score like the reference streaming.
# Without XNNPACK only the kernels differ, so one phrase covers that fallback.
@pytest.mark.parametrize(
    ("xnnpack", "fixture_names"),
    [(True, _PHRASE_FIXTURES), (False, ("okay_nabu.wav",))],
    ids=["xnnpack", "builtin-kernels"],
)
def test_engine_scores_real_audio_like_pyopen_wakeword(
    reference_scores: dict[str, list[dict[str, float]]],
    xnnpack: bool,
    fixture_names: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import _openwakeword

    if not xnnpack:
        monkeypatch.setattr(_openwakeword._TfLite, "create_xnnpack_delegate", lambda _self: None)
    for fixture_name in fixture_names:
        expected = reference_scores[fixture_name]
        observed: list[dict[str, float]] = []
        engine = WakewordModelCatalog(FIXTURES / "settings.json").create_engine(
            [PhraseConfig(model_id) for model_id in DEFAULT_MODEL_IDS],
            score_listener=observed.append,
        )
        engine.start()
        try:
            for chunk in _chunks(FIXTURES / fixture_name):
                engine.detect(chunk)
        finally:
            engine.stop()

        assert len(observed) == len(expected)
        for chunk_scores, reference in zip(observed, expected, strict=True):
            assert chunk_scores == pytest.approx(reference, abs=1e-3)


def _pyopen_model_bytes(name: str) -> bytes:
    import pyopen_wakeword

    return (Path(pyopen_wakeword.__file__).parent / "models" / name).read_bytes()


# Only openWakeWord phrase heads (embedding windows in, one probability out) are
# importable; another TFLite model would load but score nonsense at runtime.
@pytest.mark.parametrize(
    ("content", "accepted"),
    [
        (lambda: engine_module._BUNDLED_HEY_NABU_PATH.read_bytes(), True),
        (lambda: _pyopen_model_bytes("melspectrogram.tflite"), False),
        (lambda: b"TFL3" + b"\0" * 64, False),
    ],
    ids=["phrase-head", "other-tflite-model", "corrupt"],
)
def test_catalog_imports_only_real_phrase_models(
    real_models: None, tmp_path: Path, content: Callable[[], bytes], accepted: bool
) -> None:
    catalog = WakewordModelCatalog(tmp_path / "settings.json")

    if accepted:
        imported = catalog.import_model("phrase.tflite", content())
        engine = catalog.create_engine([PhraseConfig(imported.id)])
        engine.start()
        engine.stop()
    else:
        with pytest.raises(WakewordModelError) as error:
            catalog.import_model("phrase.tflite", content())
        assert error.value.error_code == "wakeword_model_invalid"
        assert list((tmp_path / "wakewords").iterdir()) == []
