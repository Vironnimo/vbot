"""Tests for MockWakewordEngine score sequence and lifecycle."""

from __future__ import annotations

from desktop.wakeword.engine import MockWakewordEngine


def test_mock_engine_returns_zero_when_not_running() -> None:
    engine = MockWakewordEngine()

    score = engine.detect(b"\x00" * 2560)

    assert score == 0.0


def test_mock_engine_returns_scores_from_sequence() -> None:
    engine = MockWakewordEngine(score_sequence=[0.2, 0.5, 0.9])
    engine.start()

    assert engine.detect(b"\x00" * 2560) == 0.2
    assert engine.detect(b"\x00" * 2560) == 0.5
    assert engine.detect(b"\x00" * 2560) == 0.9


def test_mock_engine_wraps_around_sequence() -> None:
    engine = MockWakewordEngine(score_sequence=[0.0, 1.0])
    engine.start()

    assert engine.detect(b"\x00" * 2560) == 0.0
    assert engine.detect(b"\x00" * 2560) == 1.0
    assert engine.detect(b"\x00" * 2560) == 0.0
    assert engine.detect(b"\x00" * 2560) == 1.0


def test_mock_engine_stops_returning_after_stop() -> None:
    engine = MockWakewordEngine(score_sequence=[0.8])
    engine.start()
    assert engine.detect(b"\x00" * 2560) == 0.8

    engine.stop()
    assert engine.detect(b"\x00" * 2560) == 0.0


def test_mock_engine_default_sequence_returns_zero() -> None:
    engine = MockWakewordEngine()
    engine.start()

    for _ in range(10):
        assert engine.detect(b"\x00" * 2560) == 0.0


def test_set_score_sequence_replaces_and_resets() -> None:
    engine = MockWakewordEngine(score_sequence=[0.9, 0.9, 0.9])
    engine.start()
    engine.detect(b"\x00" * 2560)  # 0.9
    engine.detect(b"\x00" * 2560)  # 0.9

    engine.set_score_sequence([0.1])

    assert engine.detect(b"\x00" * 2560) == 0.1


def test_mock_engine_accepts_any_audio_chunk_size() -> None:
    engine = MockWakewordEngine(score_sequence=[0.5])
    engine.start()

    assert engine.detect(b"\x00" * 480) == 0.5  # Small chunk
    assert engine.detect(b"\x00" * 2560) == 0.5  # Standard chunk
    assert engine.detect(b"" * 0) == 0.5  # Empty chunk
