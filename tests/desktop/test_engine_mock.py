"""Tests for MockWakewordEngine structured matches and lifecycle."""

from desktop.wakeword.engine import MockWakewordEngine, WakewordMatch


def test_mock_engine_returns_none_before_start() -> None:
    engine = MockWakewordEngine(score_sequence=[1.0])

    assert engine.detect(b"audio") is None


def test_mock_engine_cycles_scores_and_returns_only_threshold_matches() -> None:
    engine = MockWakewordEngine(
        score_sequence=[0.2, 0.5, 0.9],
        model_id="builtin/hey_nabu",
    )
    engine.start()

    assert engine.detect(b"audio") is None
    assert engine.detect(b"audio") == WakewordMatch("builtin/hey_nabu", 0.5, 0.5)
    assert engine.detect(b"audio") == WakewordMatch("builtin/hey_nabu", 0.9, 0.5)
    assert engine.detect(b"audio") is None


def test_mock_engine_stop_and_score_replacement_reset_state() -> None:
    engine = MockWakewordEngine(score_sequence=[0.9, 0.9])
    engine.start()
    assert engine.detect(b"audio") is not None

    engine.set_score_sequence([0.1, 1.0])
    assert engine.detect(b"audio") is None
    assert engine.detect(b"audio") is not None

    engine.stop()
    assert engine.detect(b"audio") is None
