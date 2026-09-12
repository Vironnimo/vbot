"""Worker: recording behavior."""

from __future__ import annotations

import io
import wave
from unittest.mock import MagicMock

import pytest

from desktop.wakeword.engine import MockWakewordEngine, WakewordMatch
from tests.desktop.worker_helpers import (
    DetectOnceEngine,
    EndlessNoiseStream,
    FakeBridge,
    FakeSounddeviceStream,
    _make_silence_chunk,
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


class EndlessSpeechStream:
    """Yields endless 32 ms loud chunks: the detector face-classifies as speech."""

    def read_pcm16(self, _frame_size: int) -> bytes:
        return _make_speech_chunk()


def test_encode_wav_produces_valid_container() -> None:
    """WAV encoding should produce a playable header with correct PCM data."""
    import io
    import wave

    from desktop.wakeword._audio_capture import (
        _encode_wav,
    )

    raw = _make_silence_chunk(1600)  # 100ms of silence
    wav_bytes = _encode_wav(raw)

    buffer = io.BytesIO(wav_bytes)
    with wave.open(buffer, "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.readframes(wf.getnframes()) == raw


def test_detection_loop_passes_the_last_four_chunks_as_pre_roll(
    fake_bridge: FakeBridge,
) -> None:
    from desktop.wakeword.worker import CapturedAudioFrame, WakewordWorker

    chunks = [bytes([value]) * 2560 for value in range(1, 6)]

    class DetectFifthEngine:
        def __init__(self) -> None:
            self.calls = 0

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def detect(self, _chunk: bytes, *, speech_present: bool = True) -> WakewordMatch | None:
            self.calls += 1
            if self.calls == 5:
                return WakewordMatch("builtin/hey_nabu", 0.8, 0.5)
            return None

    worker = WakewordWorker(
        engine=DetectFifthEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    captured: list[bytes] = []
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    worker._open_stream = lambda: setattr(  # type: ignore[method-assign]
        worker, "_stream", FakeSounddeviceStream(chunks)
    )

    def handle(pre_roll: bytes | tuple[CapturedAudioFrame, ...]) -> None:
        if isinstance(pre_roll, bytes):
            captured.append(pre_roll)
        else:
            captured.append(b"".join(frame.detection_pcm16 for frame in pre_roll))
        worker._running.clear()

    worker._handle_detection = handle  # type: ignore[assignment,method-assign]
    worker._running.set()

    worker._run()

    assert captured == [b"".join(chunks[-4:])]
    assert fake_bridge.states == ["listening", "wakeword_detected"]


def test_calibration_suppresses_wakeword_activation(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import WakewordWorker

    engine = DetectOnceEngine()
    worker = WakewordWorker(
        engine=engine,
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        calibration_checker=lambda: True,
    )
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._target_agent_available = lambda _agent_id: True  # type: ignore[assignment,method-assign]
    worker._open_stream = lambda: setattr(  # type: ignore[method-assign]
        worker,
        "_stream",
        FakeSounddeviceStream([_make_silence_chunk()], on_read=worker._running.clear),
    )
    worker._running.set()

    worker._run()

    assert engine.calls == 1
    assert "wakeword_detected" not in fake_bridge.states
    assert fake_bridge.states == ["listening"]


def test_recording_prepends_end_aligned_pre_roll_when_new_speech_arrives(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword.worker import WakewordWorker

    class SpeechFirstDetector:
        def __init__(self) -> None:
            self.calls = 0

        def reset(self) -> None:
            self.calls = 0

        def is_speech(self, _frame: bytes) -> bool:
            self.calls += 1
            return self.calls == 1

    frame_bytes = 512 * 2
    aligned_pre_roll = b"".join(bytes([value]) * frame_bytes for value in range(1, 11))
    pre_roll = b"x" * 64 + aligned_pre_roll
    speech = b"s" * frame_bytes
    silence = b"\0" * frame_bytes
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=SpeechFirstDetector(),
    )
    worker._stream = FakeSounddeviceStream([speech, *([silence] * 50)])
    worker._running.set()

    wav_bytes = worker._record_until_silence(pre_roll)

    assert wav_bytes is not None
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        recorded = wav_file.readframes(wav_file.getnframes())
    assert recorded.startswith(aligned_pre_roll + speech)


def test_pre_roll_alone_does_not_become_a_command(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    class SilentDetector:
        def reset(self) -> None:
            pass

        def is_speech(self, _frame: bytes) -> bool:
            return False

    monkeypatch.setattr(worker_module, "_SPEECH_START_FRAME_COUNT", 2)
    wake_phrase_audio = b"w" * (512 * 2 * 10)
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=SilentDetector(),
    )
    worker._running.set()

    # _record_until_silence consumes 512-sample PCM frames from the stream.
    class EndlessSilenceStream:
        def read_pcm16(self, _frame_size: int) -> bytes:
            return b"\0" * 1024

    worker._stream = EndlessSilenceStream()

    assert worker._record_until_silence(wake_phrase_audio) is None


def test_recording_read_failure_discards_partial_command(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import MicrophoneCaptureError, WakewordWorker

    class AlwaysSpeechDetector:
        def reset(self) -> None:
            pass

        def is_speech(self, _frame: bytes) -> bool:
            return True

    class PartialThenFailStream:
        def __init__(self) -> None:
            self.reads = 0

        def read_pcm16(self, _frame_size: int) -> bytes:
            self.reads += 1
            if self.reads > 1:
                raise RuntimeError("device disconnected")
            return _make_speech_chunk()

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=AlwaysSpeechDetector(),
    )
    worker._stream = PartialThenFailStream()
    worker._read_config = lambda: {"target_agent_id": "main"}  # type: ignore[method-assign]
    worker._upload_budget_pcm16_bytes = 1_000_000
    transcribe = MagicMock(return_value="partial command")
    worker._transcribe = transcribe  # type: ignore[method-assign]
    worker._running.set()

    with pytest.raises(MicrophoneCaptureError):
        worker._handle_detection()

    transcribe.assert_not_called()


def test_recording_exceeds_15_seconds_when_speech_continues(
    fake_bridge: FakeBridge,
) -> None:
    """Continuous speech must not be truncated by any fixed duration cap.

    Regression for the retired 15-second hard limit: a user who keeps talking
    is recorded until the utterance really ends; only the speech upload size
    budget (or silence) ends the capture.
    """
    from desktop.wakeword._worker_constants import (
        _SILENCE_FRAME_COUNT,
        _VAD_FRAME_DURATION_MS,
    )
    from desktop.wakeword.worker import (
        WakewordWorker,
    )

    class AlwaysSpeechDetector:
        def reset(self) -> None:
            pass

        def is_speech(self, _frame: bytes) -> bool:
            return True

    frames_for_20_seconds = int(20.0 / (_VAD_FRAME_DURATION_MS / 1000))
    speech_chunk = _make_speech_chunk()
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=AlwaysSpeechDetector(),
    )
    worker._stream = FakeSounddeviceStream([speech_chunk] * frames_for_20_seconds)
    worker._running.set()

    wav_bytes = worker._record_until_silence()

    assert wav_bytes is not None
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        duration_seconds = wav_file.getnframes() / wav_file.getframerate()
    assert duration_seconds >= 20.0 - _SILENCE_FRAME_COUNT * (_VAD_FRAME_DURATION_MS / 1000)


def test_recording_stops_at_upload_budget_when_speech_never_ends(
    fake_bridge: FakeBridge,
) -> None:
    """An endless "speech" classification ends at the upload budget, not never.

    The budget is the conservative payload slice of the active server speech
    upload limit, measured in native-rate PCM bytes; the recording may never
    produce a payload the server would reject as oversize.
    """
    from desktop.wakeword._worker_constants import (
        _SPEECH_UPLOAD_LIMIT_SAFETY_MARGIN_FRACTION,
        _UPLOAD_BUDGET_FALLBACK_BYTES,
        _WAV_HEADER_BYTES,
    )
    from desktop.wakeword.worker import (
        WakewordWorker,
    )

    class AlwaysSpeechDetector:
        def reset(self) -> None:
            pass

        def is_speech(self, _frame: bytes) -> bool:
            return True

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=AlwaysSpeechDetector(),
    )
    worker._rpc_call = lambda _method, _params: {  # type: ignore[assignment]
        "setting": {"value": _UPLOAD_BUDGET_FALLBACK_BYTES}
    }
    worker._stream = EndlessSpeechStream()
    worker._running.set()

    wav_bytes = worker._record_until_silence()

    assert wav_bytes is not None
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        payload_bytes = wav_file.getnframes() * wav_file.getsampwidth()
    assert payload_bytes <= int(
        (_UPLOAD_BUDGET_FALLBACK_BYTES - _WAV_HEADER_BYTES)
        * _SPEECH_UPLOAD_LIMIT_SAFETY_MARGIN_FRACTION
    )


def test_upload_budget_asks_the_server_for_its_active_limit(
    fake_bridge: FakeBridge,
) -> None:
    """The recording budget derives from the server's configured upload limit."""
    from desktop.wakeword._worker_constants import (
        _SPEECH_UPLOAD_LIMIT_SAFETY_MARGIN_FRACTION,
        _WAV_HEADER_BYTES,
    )
    from desktop.wakeword.worker import (
        WakewordWorker,
    )

    server_limit = 10 * 1024 * 1024
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._rpc_call = lambda _method, _params: {  # type: ignore[assignment]
        "setting": {"value": server_limit}
    }

    budget = worker._resolve_upload_budget_bytes()

    assert budget == int(
        (server_limit - _WAV_HEADER_BYTES) * _SPEECH_UPLOAD_LIMIT_SAFETY_MARGIN_FRACTION
    )


def test_stop_recording_ends_capture_and_keeps_audio(
    fake_bridge: FakeBridge,
) -> None:
    """A user stop closes the capture early but keeps the audio recorded so far.

    The captured frames must still flow through the normal pipeline (the caller
    transcribes and sends them), so the recording returns WAV bytes instead of
    discarding the utterance.
    """
    from desktop.wakeword.worker import WakewordWorker

    class AlwaysSpeechDetector:
        def reset(self) -> None:
            pass

        def is_speech(self, _frame: bytes) -> bool:
            return True

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=AlwaysSpeechDetector(),
    )
    reads = {"count": 0}

    def stop_after_few_reads() -> None:
        reads["count"] += 1
        if reads["count"] >= 5:
            worker.stop_recording()

    worker._stream = FakeSounddeviceStream(
        [_make_speech_chunk()] * 1000,
        on_read=stop_after_few_reads,
    )
    worker._running.set()

    wav_bytes = worker._record_until_silence()

    assert wav_bytes is not None
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        duration_seconds = wav_file.getnframes() / wav_file.getframerate()
    # A handful of frames plus pre-roll, far below the endless stream.
    assert duration_seconds < 1.0


def test_stop_recording_request_does_not_leak_into_next_recording(
    fake_bridge: FakeBridge,
) -> None:
    """A stale stop request must not cut the next recording short.

    The stop event is cleared before every recording starts, so a stop that
    already ended one utterance never breaks the following one.
    """
    from unittest.mock import MagicMock

    from desktop.wakeword.worker import WakewordWorker

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._stop_recording.set()
    worker._read_config = lambda: {  # type: ignore[method-assign]
        "target_agent_id": "main",
        "session_behavior": "active",
    }
    worker._stream = FakeSounddeviceStream([_make_speech_chunk()] * 10)
    worker._transcribe = lambda _audio: "test command"  # type: ignore[assignment,method-assign]
    worker._resolve_session = MagicMock(return_value="session-1")  # type: ignore[method-assign]
    worker._send_transcript = MagicMock(return_value=True)  # type: ignore[method-assign]
    worker._running.set()

    outcome = worker._handle_detection()

    assert outcome == "sent"
    worker._send_transcript.assert_called_once()


def test_recording_ends_during_continuous_noise_not_at_max_duration(
    fake_bridge: FakeBridge,
) -> None:
    """Continuous ambient noise must close the recording after silence, not hold it.

    Regression for wind/rain/traffic keeping the channel open: the neural
    detector classifies every ambient frame as non-speech, so after the
    wake-word-adjacent speech the recording ends at the silence timeout even
    though noise continues forever.
    """
    from desktop.wakeword._worker_constants import (
        _SILENCE_FRAME_COUNT,
    )
    from desktop.wakeword.worker import (
        WakewordWorker,
    )

    class NoiseDetector:
        """Classifies only the first two frames as speech, then pure noise."""

        def __init__(self) -> None:
            self.calls = 0

        def reset(self) -> None:
            self.calls = 0

        def is_speech(self, _frame: bytes) -> bool:
            self.calls += 1
            return self.calls <= 2

    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
        speech_detector=NoiseDetector(),
    )
    # Endless ambient noise after the initial speech; the recording must still
    # end at the silence timeout.
    worker._stream = EndlessNoiseStream(_make_speech_chunk())
    worker._running.set()

    wav_bytes = worker._record_until_silence()

    assert wav_bytes is not None
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        frame_count = wav_file.getnframes()
    # 2 speech frames + a full silence window; never the endless noise tail.
    expected_max_samples = (2 + _SILENCE_FRAME_COUNT + 1) * 512
    assert frame_count <= expected_max_samples
