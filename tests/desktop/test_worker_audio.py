"""Worker: audio behavior."""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from tests.desktop.worker_helpers import (
    no_real_neural_speech_detector as no_real_neural_speech_detector,
)
from tests.desktop.worker_helpers import (
    ready_speech_to_text as ready_speech_to_text,
)


@pytest.mark.parametrize(
    ("transcript", "cancelled"),
    [
        ("Abbrechen.", True),
        ("Mach das Licht aus, ach nein, vergiss es!", True),
        ("Bitte erkläre mir diesen Satz", False),
        ("Abbrechen und danach fortfahren", False),
    ],
)
def test_voice_cancel_phrase_requires_reserved_ending(transcript: str, cancelled: bool) -> None:
    from desktop.wakeword._worker_support import (
        _is_voice_cancel_phrase,
    )

    assert _is_voice_cancel_phrase(transcript) is cancelled


def test_resampling_stream_normalizes_native_rate_to_wakeword_pcm() -> None:
    import numpy as np

    pytest.importorskip("soxr")
    from desktop.wakeword.worker import CaptureFormat, ResamplingInputStream

    class NativeStream:
        @staticmethod
        def read(frame_count: int) -> tuple[object, bool]:
            return np.linspace(-0.5, 0.5, frame_count, dtype=np.float32)[:, None], False

    stream = ResamplingInputStream(
        NativeStream(),  # type: ignore[arg-type]
        CaptureFormat(device=4, name="Studio mic", sample_rate=48000, dtype="float32"),
    )

    frame = stream.read_capture_frame(1280)

    assert len(frame.detection_pcm16) == 1280 * 2
    assert len(frame.recording_pcm16) == 3840 * 2
    assert frame.recording_sample_rate == 48000
    samples = np.frombuffer(frame.detection_pcm16, dtype=np.int16)
    assert samples[0] < 0
    assert samples[-1] > 0


def test_resampling_stream_attenuates_out_of_band_tones() -> None:
    import numpy as np

    pytest.importorskip("soxr")
    from desktop.wakeword.worker import CaptureFormat, ResamplingInputStream

    class SineNativeStream:
        def __init__(self, frequency: int, rate: int, amplitude: float = 0.5) -> None:
            self._frequency = frequency
            self._rate = rate
            self._amplitude = amplitude
            self._position = 0

        def read(self, frame_count: int) -> tuple[object, bool]:
            end = self._position + frame_count
            samples = (
                np.sin(2 * np.pi * self._frequency * np.arange(self._position, end) / self._rate)
                * self._amplitude
            )
            self._position = end
            return samples.astype(np.float32)[:, None], False

    def detection_rms(pcm16: bytes) -> float:
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
        return float(np.sqrt(np.mean(samples**2)))

    def read_detection_rms(frequency: int) -> float:
        stream = ResamplingInputStream(
            SineNativeStream(frequency, 48000),  # type: ignore[arg-type]
            CaptureFormat(device=4, name="Studio mic", sample_rate=48000, dtype="float32"),
        )
        frames = [stream.read_capture_frame(1280) for _ in range(12)]
        # Skip the resampler's startup latency before measuring.
        return detection_rms(b"".join(frame.detection_pcm16 for frame in frames[2:]))

    # A 10 kHz tone is out of band for the 16 kHz detector and must be filtered
    # away instead of aliasing into the detector's spectrum; 1 kHz must survive.
    out_of_band_rms = read_detection_rms(10000)
    in_band_rms = read_detection_rms(1000)

    assert in_band_rms > 8000
    assert out_of_band_rms < in_band_rms / 8


def test_resampling_stream_returns_exact_detection_length_every_read() -> None:
    import numpy as np

    pytest.importorskip("soxr")
    from desktop.wakeword.worker import CaptureFormat, ResamplingInputStream

    class NoisyNativeStream:
        def __init__(self, rate: int) -> None:
            self._rate = rate
            self._position = 0

        def read(self, frame_count: int) -> tuple[object, bool]:
            end = self._position + frame_count
            samples = np.sin(2 * np.pi * 440 * np.arange(self._position, end) / self._rate)
            self._position = end
            return samples.astype(np.float32)[:, None], False

    stream = ResamplingInputStream(
        NoisyNativeStream(44100),  # type: ignore[arg-type]
        CaptureFormat(device=4, name="Studio mic", sample_rate=44100, dtype="float32"),
    )

    lengths = {len(stream.read_capture_frame(1280).detection_pcm16) for _ in range(24)}

    assert lengths == {1280 * 2}


@pytest.mark.parametrize("sample_rate", [32000, 44100, 48000])
def test_resampling_stream_preserves_continuous_audio_across_mixed_reads(
    sample_rate: int,
) -> None:
    pytest.importorskip("soxr")
    from desktop.wakeword.worker import CaptureFormat, ResamplingInputStream

    class ContinuousToneStream:
        def __init__(self) -> None:
            self.position = 0

        def read(self, frame_count: int) -> tuple[object, bool]:
            end = self.position + frame_count
            samples = np.sin(2 * np.pi * 440 * np.arange(self.position, end) / sample_rate)
            self.position = end
            return samples.astype(np.float32)[:, None], False

    native = ContinuousToneStream()
    stream = ResamplingInputStream(
        native,
        CaptureFormat(device=4, name="Studio mic", sample_rate=sample_rate, dtype="float32"),
    )
    requested_frames = [1280, 512, 512, 1280, 512, 512, 1280]

    frames = [stream.read_capture_frame(frame_count) for frame_count in requested_frames]

    assert [len(frame.detection_pcm16) // 2 for frame in frames] == requested_frames
    assert sum(len(frame.recording_pcm16) // 2 for frame in frames) == (
        sum(requested_frames) * sample_rate // 16000
    )
    assert all(
        np.max(np.abs(np.frombuffer(frame.detection_pcm16, dtype=np.int16))) > 1000
        for frame in frames
    )


def test_resampling_stream_rejects_overflowed_audio() -> None:
    from desktop.wakeword.worker import (
        CaptureFormat,
        MicrophoneCaptureError,
        ResamplingInputStream,
    )

    class OverflowingStream:
        @staticmethod
        def read(frame_count: int) -> tuple[object, bool]:
            return np.zeros((frame_count, 1), dtype=np.int16), True

    stream = ResamplingInputStream(
        OverflowingStream(),
        CaptureFormat(device=0, name="Mic", sample_rate=16000, dtype="int16"),
    )

    with pytest.raises(MicrophoneCaptureError, match="overflowed"):
        stream.read_capture_frame(512)


def test_resample_pcm16_filters_out_of_band_audio() -> None:
    import numpy as np

    pytest.importorskip("soxr")
    from desktop.wakeword._audio_capture import (
        _resample_pcm16,
    )

    def sine_pcm16(frequency: int, rate: int) -> bytes:
        samples = (np.sin(2 * np.pi * frequency * np.arange(rate) / rate) * 12000).astype(np.int16)
        return samples.tobytes()

    def rms(pcm16: bytes) -> float:
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
        return float(np.sqrt(np.mean(samples**2)))

    in_band = _resample_pcm16(sine_pcm16(1000, 48000), 48000, 16000)
    out_of_band = _resample_pcm16(sine_pcm16(10000, 48000), 48000, 16000)

    assert len(in_band) == 16000 * 2
    assert rms(in_band) > 8000
    assert rms(out_of_band) < rms(in_band) / 8


def test_command_audio_keeps_native_capture_rate_before_server_normalization() -> None:
    from desktop.wakeword._audio_capture import (
        _encode_captured_audio,
    )
    from desktop.wakeword.worker import (
        CapturedAudioFrame,
    )

    native_audio = b"\x01\x00" * 1440
    wav_bytes = _encode_captured_audio(
        [
            CapturedAudioFrame(
                detection_pcm16=b"\x01\x00" * 480,
                recording_pcm16=native_audio,
                recording_sample_rate=48000,
            )
        ]
    )

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getframerate() == 48000
        assert wav_file.readframes(wav_file.getnframes()) == native_audio
