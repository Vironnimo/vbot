"""Bounded input decoding and target-aware preparation owned by speech execution.

Local engines consume the original recording once. Provider requests keep an
accepted original container after validation, or encode the configured fallback.
PyAV stays a lazy dependency, including for managed local speech workers.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from typing import Any, cast, override

from core.model_tasks.constants import DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS
from core.model_tasks.speech_types import SpeechError

MAX_TRANSCRIPTION_SECONDS = 30 * 60
_MAX_FRAME_BYTES = 16 * 1024 * 1024


class SpeechInputError(SpeechError, ValueError):
    """Invalid or oversized audio, before any Provider request is sent."""

    def __init__(self, message: str, *, too_large: bool = False) -> None:
        super().__init__(message)
        self.too_large = too_large


@dataclass(frozen=True)
class TranscriptionInputPolicy:
    accepted_formats: frozenset[str]
    max_audio_bytes: int = 25_000_000
    max_request_bytes: int | None = None


@dataclass(frozen=True)
class PreparedTranscriptionAudio:
    audio: bytes
    filename: str
    media_type: str


def _validated_frames(container: Any, check_cancel: Callable[[], None] | None) -> Generator[Any]:
    """Count actual decoded samples, never trusting a compressed file's duration."""
    if not container.streams.audio:
        raise SpeechInputError("Audio input has no audio stream")
    seconds = 0.0
    frames = iter(container.decode(audio=0))
    while True:
        if check_cancel is not None:
            check_cancel()
        frame = next(frames, None)
        if frame is None:
            break
        if check_cancel is not None:
            check_cancel()
        if not 0 < frame.sample_rate <= 192_000 or not 0 < len(frame.layout.channels) <= 8:
            raise SpeechInputError("Audio input has an unsupported sample rate or channel count")
        if sum(plane.buffer_size for plane in frame.planes) > _MAX_FRAME_BYTES:
            raise SpeechInputError("Decoded audio frame exceeds the size limit", too_large=True)
        seconds += frame.samples / frame.sample_rate
        if seconds > MAX_TRANSCRIPTION_SECONDS + 1e-6:
            raise SpeechInputError(
                "Audio input exceeds the 30-minute transcription limit. Split the recording.",
                too_large=True,
            )
        if frame.samples:
            yield frame
    if seconds == 0:
        raise SpeechInputError("Audio input contains no audio samples")


def iter_decoded_frames(
    audio: bytes,
    *,
    sample_rate: int,
    sample_format: str,
    check_cancel: Callable[[], None] | None = None,
) -> Generator[Any]:
    """Decode once to bounded mono frames; closing the generator closes the input."""
    import av

    try:
        if check_cancel is not None:
            check_cancel()
        with av.open(io.BytesIO(audio), mode="r") as container:
            resampler = av.AudioResampler(format=sample_format, layout="mono", rate=sample_rate)
            for frame in _validated_frames(container, check_cancel):
                yield from _resample_frames(resampler, frame)
            if check_cancel is not None:
                check_cancel()
            yield from _resample_frames(resampler, None)
    except av.error.FFmpegError as error:
        raise SpeechInputError("Audio input could not be decoded") from error


def _resample_frames(resampler: Any, frame: Any) -> list[Any]:
    try:
        return cast(list[Any], resampler.resample(frame))
    except ValueError as error:
        # PyAV reports a changing decoded format/rate/layout as ValueError,
        # not FFmpegError. This is bad input, not a failure of a local model.
        raise SpeechInputError("Audio input could not be resampled") from error


class _LimitedOutput(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit
        self.limit_error: SpeechInputError | None = None

    @override
    def write(self, data: Any) -> int:
        if self.tell() + len(data) > self._limit:
            self.limit_error = SpeechInputError(
                "Converted audio exceeds the transcription target's size limit. "
                "Use a shorter recording or a compressed transcription format.",
                too_large=True,
            )
            raise self.limit_error
        return super().write(data)


def _ebml_element(audio: bytes, offset: int, limit: int) -> tuple[int, int, int] | None:
    """Read one bounded EBML header element, without accepting unknown sizes."""
    if offset >= limit:
        return None
    id_size = 9 - audio[offset].bit_length()
    size_offset = offset + id_size
    if id_size > 4 or size_offset >= limit:
        return None
    size_size = 9 - audio[size_offset].bit_length()
    start = size_offset + size_size
    if size_size > 8 or start > limit:
        return None
    size_mask = (1 << (7 * size_size)) - 1
    size = int.from_bytes(audio[size_offset:start], "big") & size_mask
    if size == size_mask or start + size > limit:
        return None
    return int.from_bytes(audio[offset:size_offset], "big"), start, start + size


def _has_webm_header(audio: bytes) -> bool:
    # The Matroska/WebM demuxer reports both names. RFC 8794 identifies the
    # actual document type in the EBML header's DocType element (0x4282).
    header = _ebml_element(audio, 0, min(len(audio), 65_536))
    if header is None or header[0] != 0x1A45DFA3:
        return False
    _, offset, end = header
    document_type = None
    while offset < end:
        element = _ebml_element(audio, offset, end)
        if element is None:
            return False
        element_id, start, offset = element
        if element_id == 0x4282:
            if document_type is not None:
                return False
            document_type = audio[start:offset].rstrip(b"\x00")
    return document_type == b"webm"


def _has_mp4_header(audio: bytes) -> bool:
    # Shared MOV/MP4 demuxer aliases do not prove an MP4 file. Require a known
    # MP4/M4A major brand in the real ftyp box; unknown brands are converted.
    offset = 0
    limit = min(len(audio), 65_536)
    while offset + 8 <= limit:
        size = int.from_bytes(audio[offset : offset + 4], "big")
        kind = audio[offset + 4 : offset + 8]
        start = offset + 8
        if size == 1:
            if start + 8 > limit:
                return False
            size = int.from_bytes(audio[start : start + 8], "big")
            start += 8
        end = offset + size
        if end < start or end > limit:
            return False
        if kind == b"ftyp":
            return (
                end >= start + 8
                and (end - start) % 4 == 0
                and audio[start : start + 4]
                in {
                    b"isom",
                    b"iso2",
                    b"iso3",
                    b"iso4",
                    b"iso5",
                    b"iso6",
                    b"mp41",
                    b"mp42",
                    b"M4A ",
                    b"M4B ",
                }
            )
        if kind not in {b"free", b"skip", b"wide"}:
            return False
        offset = end
    return False


def _source_format(container: Any, audio: bytes) -> str | None:
    """Recognize a single audio stream from its demuxer and codec, not its name."""
    if len(container.streams) != 1 or not container.streams.audio:
        return None
    formats = set(container.format.name.split(","))
    codec = container.streams.audio[0].codec_context.name
    if "wav" in formats and codec.startswith("pcm_"):
        return "wav"
    if "flac" in formats and codec == "flac":
        return "flac"
    if "mp3" in formats and codec.startswith("mp3"):
        return "mp3"
    if "webm" in formats and codec in {"opus", "vorbis"} and _has_webm_header(audio):
        return "webm"
    if "ogg" in formats and codec in {"opus", "vorbis", "flac"}:
        return "ogg"
    if "mp4" in formats and codec in {"aac", "alac"} and _has_mp4_header(audio):
        return "m4a"
    if "aac" in formats and codec == "aac":
        return "aac"
    return None


def prepare_transcription_audio(
    audio: bytes,
    speech_settings: Mapping[str, Any],
    policy: TranscriptionInputPolicy,
    *,
    check_cancel: Callable[[], None] | None = None,
) -> PreparedTranscriptionAudio:
    """Validate the whole recording, retaining supported bytes or encoding once."""
    import av

    if check_cancel is not None:
        check_cancel()
    output: _LimitedOutput | None = None
    try:
        with av.open(io.BytesIO(audio), mode="r") as container:
            source_format = _source_format(container, audio)
            if source_format in policy.accepted_formats and len(audio) <= policy.max_audio_bytes:
                for _frame in _validated_frames(container, check_cancel):
                    pass
                return PreparedTranscriptionAudio(
                    audio, f"recording.{source_format}", _media_type(source_format)
                )

            profile = speech_settings.get("transcription_audio")
            if not isinstance(profile, Mapping):
                profile = DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS
            output_format = profile.get("format", DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS["format"])
            rate = profile.get(
                "sample_rate_hz", DEFAULT_TRANSCRIPTION_AUDIO_SETTINGS["sample_rate_hz"]
            )
            if output_format not in {"wav", "flac"} or output_format not in policy.accepted_formats:
                raise SpeechInputError(
                    "Transcription conversion format is not supported by the target"
                )
            if type(rate) is not int or rate not in {16_000, 24_000, 48_000}:
                raise SpeechInputError("Unsupported transcription audio sample rate")
            output = _LimitedOutput(policy.max_audio_bytes)
            with av.open(output, mode="w", format=output_format) as encoded:
                stream = cast(
                    Any,
                    encoded.add_stream(
                        {"wav": "pcm_s16le", "flac": "flac"}[output_format], rate=rate
                    ),
                )
                stream.layout = "mono"
                resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)

                def emit(frame: Any) -> None:
                    if check_cancel is not None:
                        check_cancel()
                    for packet in stream.encode(frame):
                        encoded.mux(packet)

                for frame in _validated_frames(container, check_cancel):
                    for converted in _resample_frames(resampler, frame):
                        emit(converted)
                for converted in _resample_frames(resampler, None):
                    emit(converted)
                emit(None)
            result = output.getvalue()
            if not result or len(result) > policy.max_audio_bytes:
                raise SpeechInputError(
                    "Converted audio exceeds the target's size limit", too_large=True
                )
            return PreparedTranscriptionAudio(
                result, f"recording.{output_format}", _media_type(output_format)
            )
    except av.error.FFmpegError as error:
        # PyAV can translate a Python file callback's exception into an FFmpeg
        # mux/close error. Keep the input-limit classification in that case.
        if output is not None and output.limit_error is not None:
            raise output.limit_error from error
        raise SpeechInputError("Audio input could not be decoded or converted") from error


def _media_type(audio_format: str) -> str:
    return {"mp3": "audio/mpeg", "m4a": "audio/mp4"}.get(audio_format, f"audio/{audio_format}")
