"""Audio capture."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Any

import numpy as np

from desktop.wakeword._worker_constants import (
    _CHANNELS,
    _MAX_RESAMPLER_FILL_READS,
    _SAMPLE_RATE,
    _SAMPLE_WIDTH,
    _VAD_FRAME_SIZE,
)


class MicrophoneUnavailableError(RuntimeError):
    """No usable input-device format could supply wakeword-quality audio."""


class MicrophoneCaptureError(RuntimeError):
    """A live input stream could not provide trustworthy command audio."""


@dataclass(frozen=True)
class CaptureFormat:
    """Concrete device format used before conversion to 16 kHz PCM."""

    device: int
    name: str
    sample_rate: int
    dtype: str
    host_api: str = ""


@dataclass(frozen=True)
class CapturedAudioFrame:
    """One microphone read projected for detection and command recording."""

    detection_pcm16: bytes
    recording_pcm16: bytes
    recording_sample_rate: int


class ResamplingInputStream:
    """Read a native sounddevice stream as 16 kHz mono signed PCM frames.

    Native-rate command audio is preserved untouched; the 16 kHz detection
    projection is produced by a stateful soxr stream whose anti-aliasing filter
    keeps out-of-band device noise (fans, hiss) out of the detector's spectrum.
    """

    def __init__(self, stream: Any, capture_format: CaptureFormat) -> None:
        self._stream = stream
        self.capture_format = capture_format
        self._resampler = _create_soxr_resampler(capture_format.sample_rate)
        self._detection_samples = np.empty(0, dtype=np.int16)
        self._recording_samples = np.empty(0, dtype=np.int16)
        self._native_frame_remainder = 0

    def start(self) -> None:
        self._stream.start()

    def read_pcm16(self, target_frames: int) -> bytes:
        return self.read_capture_frame(target_frames).detection_pcm16

    def read_capture_frame(self, target_frames: int) -> CapturedAudioFrame:
        """Read native command audio plus its 16 kHz detection projection."""

        if target_frames <= 0:
            raise ValueError("target_frames must be positive")
        native_numerator = (
            target_frames * self.capture_format.sample_rate + self._native_frame_remainder
        )
        native_frames, self._native_frame_remainder = divmod(native_numerator, _SAMPLE_RATE)
        native_frames = max(1, native_frames)

        fill_reads = 0
        while (
            len(self._detection_samples) < target_frames
            or len(self._recording_samples) < native_frames
        ):
            fill_reads += 1
            if fill_reads > _MAX_RESAMPLER_FILL_READS:
                raise MicrophoneCaptureError("Audio resampler stopped producing output")
            self._read_native_samples(native_frames)

        detection_pcm = self._detection_samples[:target_frames]
        native_pcm = self._recording_samples[:native_frames]
        self._detection_samples = self._detection_samples[target_frames:]
        self._recording_samples = self._recording_samples[native_frames:]

        return CapturedAudioFrame(
            detection_pcm16=bytes(detection_pcm.tobytes()),
            recording_pcm16=bytes(native_pcm.tobytes()),
            recording_sample_rate=self.capture_format.sample_rate,
        )

    def _read_native_samples(self, frame_count: int) -> None:
        """Append one native read and every resampler output sample to the FIFOs."""
        audio, overflowed = self._stream.read(frame_count)
        if overflowed:
            raise MicrophoneCaptureError("Microphone input overflowed")
        samples = np.asarray(audio).reshape(-1)
        if len(samples) != frame_count:
            raise MicrophoneCaptureError(
                f"Microphone returned {len(samples)} samples instead of {frame_count}"
            )
        if self.capture_format.dtype == "float32":
            normalized = np.clip(samples.astype(np.float32), -1.0, 1.0)
            native_pcm = np.clip(normalized * 32767.0, -32768, 32767).astype(np.int16)
        else:
            native_pcm = samples.astype(np.int16)
        detection_pcm = (
            native_pcm
            if self._resampler is None
            else np.asarray(self._resampler.resample_chunk(native_pcm), dtype=np.int16)
        )
        self._recording_samples = np.concatenate((self._recording_samples, native_pcm))
        self._detection_samples = np.concatenate((self._detection_samples, detection_pcm))

    def stop(self) -> None:
        self._stream.stop()

    def close(self) -> None:
        self._stream.close()


def _read_capture_frame(stream: Any, target_frames: int) -> CapturedAudioFrame:
    reader = getattr(stream, "read_capture_frame", None)
    if callable(reader):
        frame = reader(target_frames)
        if isinstance(frame, CapturedAudioFrame):
            return frame
        raise TypeError("Capture stream returned an invalid audio frame")
    detection_pcm = stream.read_pcm16(target_frames)
    return CapturedAudioFrame(
        detection_pcm16=detection_pcm,
        recording_pcm16=detection_pcm,
        recording_sample_rate=_SAMPLE_RATE,
    )


def _detection_audio_bytes(
    audio: bytes | tuple[CapturedAudioFrame, ...],
) -> bytes:
    if isinstance(audio, bytes):
        return audio
    return b"".join(frame.detection_pcm16 for frame in audio)


def _encode_captured_audio(frames: list[CapturedAudioFrame]) -> bytes:
    recording_sample_rate = frames[-1].recording_sample_rate
    raw_frames = b"".join(
        _resample_pcm16(
            frame.recording_pcm16,
            frame.recording_sample_rate,
            recording_sample_rate,
        )
        for frame in frames
    )
    return _encode_wav(raw_frames, sample_rate=recording_sample_rate)


def _resample_pcm16(audio: bytes, source_rate: int, target_rate: int) -> bytes:
    if not audio or source_rate == target_rate:
        return audio

    import numpy as np
    import soxr  # type: ignore[import-untyped]

    samples = np.frombuffer(audio, dtype=np.int16)
    return bytes(soxr.resample(samples, source_rate, target_rate).tobytes())


def _create_soxr_resampler(source_rate: int) -> Any | None:
    """Create a stateful int16 resampler, or None when capture is already 16 kHz."""
    if source_rate == _SAMPLE_RATE:
        return None

    import soxr  # type: ignore[import-untyped]

    return soxr.ResampleStream(source_rate, _SAMPLE_RATE, _CHANNELS, dtype="int16")


def _encode_wav(raw_frames: bytes, *, sample_rate: int = _SAMPLE_RATE) -> bytes:
    """Wrap mono 16-bit PCM frames in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(_CHANNELS)
        wav_file.setsampwidth(_SAMPLE_WIDTH)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(raw_frames)
    return buffer.getvalue()


def _end_aligned_vad_frames(audio: bytes) -> list[bytes]:
    """Split PCM into end-aligned full VAD frames, dropping incomplete leading audio."""
    frame_bytes = _VAD_FRAME_SIZE * _SAMPLE_WIDTH
    return [
        audio[offset : offset + frame_bytes]
        for offset in range(len(audio) % frame_bytes, len(audio), frame_bytes)
    ]
