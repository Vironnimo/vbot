"""Worker: dispatch behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from desktop.wakeword.engine import MockWakewordEngine
from tests.desktop.worker_helpers import (
    FakeBridge,
    FakeSounddeviceStream,
    _make_speech_chunk,
)
from tests.desktop.worker_helpers import (
    fake_bridge as fake_bridge,
)
from tests.desktop.worker_helpers import (
    no_real_neural_speech_detector as no_real_neural_speech_detector,
)
from tests.desktop.worker_helpers import (
    ready_speech_to_text as ready_speech_to_text,
)


def test_handle_detection_discards_voice_cancel_before_session_resolution(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._record_until_silence = lambda _pre_roll=b"": b"audio"  # type: ignore[assignment,method-assign]
    worker._transcribe = lambda _audio: "Mach das Licht aus, vergiss es."  # type: ignore[assignment,method-assign]
    worker._resolve_session = MagicMock()  # type: ignore[method-assign]
    worker._send_transcript = MagicMock()  # type: ignore[method-assign]

    outcome = worker._handle_detection()

    assert outcome == "cancelled"
    worker._resolve_session.assert_not_called()
    worker._send_transcript.assert_not_called()


def test_worker_does_not_record_without_target_agent(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._running.set()
    worker._read_config = lambda: {"target_agent_id": None}  # type: ignore[method-assign]

    worker._handle_detection()

    assert fake_bridge.states == ["error"]
    assert not worker._running.is_set()


def test_handle_detection_closes_microphone_before_network_calls(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._stream = stream
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._record_until_silence = lambda _pre_roll=b"": b"audio"  # type: ignore[assignment,method-assign]

    def transcribe(_audio_data: bytes) -> str:
        assert worker._stream is None
        return "hello"

    def resolve_session(_agent_id: str, _behavior: str) -> str:
        assert worker._stream is None
        return "session-one"

    def send_transcript(_transcript: str, _agent_id: str, _session_id: str) -> bool:
        assert worker._stream is None
        return True

    worker._transcribe = transcribe  # type: ignore[assignment,method-assign]
    worker._resolve_session = resolve_session  # type: ignore[assignment,method-assign]
    worker._send_transcript = send_transcript  # type: ignore[assignment,method-assign]

    worker._handle_detection()

    assert stream.stopped is True
    assert stream.closed is True
    assert fake_bridge.states == ["recording", "transcribing", "sending"]
    assert worker._running.is_set()


def test_handle_detection_empty_transcript_returns_to_listening(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._stream = stream
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._record_until_silence = lambda _pre_roll=b"": b"audio"  # type: ignore[assignment,method-assign]
    worker._transcribe = lambda _audio_data: "   "  # type: ignore[assignment,method-assign]
    worker._resolve_session = MagicMock()  # type: ignore[method-assign]
    worker._send_transcript = MagicMock()  # type: ignore[method-assign]

    worker._handle_detection()

    worker._resolve_session.assert_not_called()
    worker._send_transcript.assert_not_called()
    assert fake_bridge.states == ["recording", "transcribing"]
    assert worker._running.is_set()


def test_handle_detection_transcription_failure_returns_to_listening(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._record_until_silence = lambda _pre_roll=b"": b"audio"  # type: ignore[assignment,method-assign]
    worker._transcribe = lambda _audio_data: None  # type: ignore[assignment,method-assign]
    worker._resolve_session = MagicMock()  # type: ignore[method-assign]
    worker._send_transcript = MagicMock()  # type: ignore[method-assign]

    worker._handle_detection()

    worker._resolve_session.assert_not_called()
    worker._send_transcript.assert_not_called()
    assert fake_bridge.states == ["recording", "transcribing"]
    assert worker._running.is_set()


@pytest.mark.parametrize("transcript", [None, "   "])
def test_handle_detection_discards_failed_outcome_when_stopped_during_transcription(
    fake_bridge: FakeBridge,
    transcript: str | None,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._record_until_silence = lambda _pre_roll=b"": b"audio"  # type: ignore[assignment,method-assign]

    def transcribe_then_stop(_audio_data: bytes) -> str | None:
        worker._running.clear()
        return transcript

    worker._transcribe = transcribe_then_stop  # type: ignore[assignment,method-assign]
    worker._resolve_session = MagicMock()  # type: ignore[method-assign]
    worker._send_transcript = MagicMock()  # type: ignore[method-assign]

    outcome = worker._handle_detection()

    assert outcome is None
    worker._resolve_session.assert_not_called()
    worker._send_transcript.assert_not_called()
    assert fake_bridge.states == ["recording", "transcribing"]


@pytest.mark.parametrize("outcome", [None, "transcription_failed"])
def test_prepare_next_listen_publishes_nothing_after_stop(
    fake_bridge: FakeBridge,
    outcome: str | None,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    fake_bridge.publish_state("off")

    worker._prepare_next_listen(outcome)

    assert fake_bridge.states == ["off"]


def test_handle_detection_skips_network_when_stopped_during_recording(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }

    def record_then_stop(_pre_roll: bytes = b"") -> bytes:
        worker._running.clear()  # disabled/reconfigured mid-recording
        return b"audio"

    worker._record_until_silence = record_then_stop  # type: ignore[assignment,method-assign]
    worker._transcribe = MagicMock()  # type: ignore[method-assign]

    worker._handle_detection()

    # Stopped during recording: no transcription round-trip at all.
    worker._transcribe.assert_not_called()
    assert fake_bridge.states == ["recording"]


def test_handle_detection_discards_transcript_when_stopped_during_transcription(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()])
    worker._running.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._record_until_silence = lambda _pre_roll=b"": b"audio"  # type: ignore[assignment,method-assign]

    def transcribe_then_stop(_audio_data: bytes) -> str:
        worker._running.clear()  # disabled mid-transcription
        return "turn on the lights"

    worker._transcribe = transcribe_then_stop  # type: ignore[assignment,method-assign]
    worker._resolve_session = MagicMock()  # type: ignore[method-assign]
    worker._send_transcript = MagicMock()  # type: ignore[method-assign]

    worker._handle_detection()

    # A stop between capture and send must not fire a now-stale command.
    worker._resolve_session.assert_not_called()
    worker._send_transcript.assert_not_called()
    assert fake_bridge.states == ["recording", "transcribing"]
