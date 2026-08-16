"""Real-audio verification for the bundled pyopen-wakeword models."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pyopen_wakeword")

from desktop.wakeword.engine import (
    DEFAULT_WAKEWORD_MODEL_IDS,
    WakewordEngine,
    WakewordMatch,
    WakewordModelCatalog,
)

_FIXTURE_DIRECTORY = Path(__file__).parents[1] / "fixtures" / "wakeword"
_CHUNK_BYTES = 1280 * 2
_SAMPLE_RATE = 16000


@pytest.mark.parametrize(
    ("model_id", "fixture_name"),
    [
        ("builtin/okay_nabu", "okay_nabu.wav"),
        ("builtin/hey_nabu", "hey_nabu.wav"),
    ],
)
def test_each_nabu_model_detects_its_positive_audio(model_id: str, fixture_name: str) -> None:
    engine = WakewordModelCatalog(_FIXTURE_DIRECTORY / "settings.json").create_engine([model_id])

    matches = _detect_file(engine, _FIXTURE_DIRECTORY / fixture_name)

    assert len(matches) == 1
    assert matches[0].model_id == model_id


@pytest.mark.parametrize("fixture_name", ["okay_nabu.wav", "hey_nabu.wav"])
def test_two_active_nabu_models_emit_one_activation_per_phrase(fixture_name: str) -> None:
    engine = WakewordModelCatalog(_FIXTURE_DIRECTORY / "settings.json").create_engine(
        list(DEFAULT_WAKEWORD_MODEL_IDS)
    )

    matches = _detect_file(engine, _FIXTURE_DIRECTORY / fixture_name)

    assert len(matches) == 1
    assert matches[0].model_id in DEFAULT_WAKEWORD_MODEL_IDS


def test_two_active_nabu_models_ignore_unrelated_wakeword_audio() -> None:
    engine = WakewordModelCatalog(_FIXTURE_DIRECTORY / "settings.json").create_engine(
        list(DEFAULT_WAKEWORD_MODEL_IDS)
    )

    matches = _detect_file(engine, _FIXTURE_DIRECTORY / "unrelated_hey_jarvis.wav")

    assert matches == []


def test_gated_positive_audio_never_activates_the_models() -> None:
    engine = WakewordModelCatalog(_FIXTURE_DIRECTORY / "settings.json").create_engine(
        list(DEFAULT_WAKEWORD_MODEL_IDS)
    )

    matches = _detect_file(engine, _FIXTURE_DIRECTORY / "okay_nabu.wav", speech_present=False)

    assert matches == []


def _detect_file(
    engine: WakewordEngine, path: Path, *, speech_present: bool = True
) -> list[WakewordMatch]:
    audio = _read_pcm16_mono(path)
    padding = np.zeros(_SAMPLE_RATE, dtype=np.int16).tobytes()
    stream = padding + audio + padding
    matches = []
    engine.start()
    try:
        for offset in range(0, len(stream), _CHUNK_BYTES):
            chunk = stream[offset : offset + _CHUNK_BYTES].ljust(_CHUNK_BYTES, b"\0")
            match = engine.detect(chunk, speech_present=speech_present)
            if match is not None:
                matches.append(match)
    finally:
        engine.stop()
    return matches


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
