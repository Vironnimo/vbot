"""Speech input limits follow decoded audio, with prompt decoder cleanup."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import av
import pytest

from core.model_tasks import speech_input
from core.model_tasks.speech_input import (
    SpeechInputError,
    TranscriptionInputPolicy,
    iter_decoded_frames,
    prepare_transcription_audio,
)

_RATE = 16_000
_FLAC_POLICY = TranscriptionInputPolicy(frozenset({"flac"}))


def _flac(samples: int) -> bytes:
    output = io.BytesIO()
    with av.open(output, mode="w", format="flac") as container:
        stream = container.add_stream("flac", rate=_RATE)
        stream.layout = "mono"
        frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
        frame.sample_rate = _RATE
        frame.planes[0].update(bytes(samples * 2))
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return output.getvalue()


@pytest.mark.parametrize(
    ("container_format", "codec", "expected_format"),
    [
        ("webm", "libopus", "webm"),
        ("matroska", "libopus", "flac"),
        ("mp4", "aac", "m4a"),
        ("ipod", "aac", "m4a"),
        ("mov", "aac", "flac"),
    ],
)
def test_original_container_is_preserved_only_when_its_actual_type_is_accepted(
    container_format: str, codec: str, expected_format: str
) -> None:
    output = io.BytesIO()
    with av.open(output, mode="w", format=container_format) as container:
        container.metadata["title"] = "webm isom M4A "
        stream = cast(Any, container.add_stream(codec, rate=48_000))
        stream.layout = "mono"
        frame = av.AudioFrame(format="fltp", layout="mono", samples=4800)
        frame.sample_rate = 48_000
        frame.planes[0].update(bytes(frame.planes[0].buffer_size))
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    original = output.getvalue()
    prepared = prepare_transcription_audio(
        original,
        {"transcription_audio": {"format": "flac", "sample_rate_hz": _RATE}},
        TranscriptionInputPolicy(frozenset({"webm", "m4a", "flac"})),
    )

    assert prepared.filename == f"recording.{expected_format}"
    assert (prepared.audio == original) == (expected_format != "flac")
    with av.open(io.BytesIO(prepared.audio), mode="r") as container:
        assert sum(frame.samples for frame in container.decode(audio=0)) > 0


@pytest.mark.parametrize("route", ["local", "provider"])
def test_changing_decoded_sample_rate_is_an_input_error(route: str) -> None:
    segments = []
    for rate in (44_100, 48_000):
        output = io.BytesIO()
        with av.open(output, mode="w", format="mp3") as container:
            stream = container.add_stream("libmp3lame", rate=rate)
            stream.layout = "mono"
            frame = av.AudioFrame(format="fltp", layout="mono", samples=rate // 10)
            frame.sample_rate = rate
            frame.planes[0].update(bytes(frame.planes[0].buffer_size))
            for packet in stream.encode(frame):
                container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
        segments.append(output.getvalue())
    audio = b"".join(segments)

    with pytest.raises(SpeechInputError) as error:
        if route == "provider":
            prepare_transcription_audio(audio, {}, _FLAC_POLICY)
        else:
            list(iter_decoded_frames(audio, sample_rate=_RATE, sample_format="fltp"))
    assert not error.value.too_large


@pytest.mark.parametrize("route", ["local", "provider"])
def test_duration_limit_counts_decoded_samples_including_the_exact_boundary(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    monkeypatch.setattr(speech_input, "MAX_TRANSCRIPTION_SECONDS", 0.1)
    exact, over = _flac(1600), _flac(1601)
    open_container = av.open

    @contextmanager
    def misleading_duration(*args: Any, **kwargs: Any) -> Iterator[Any]:
        with open_container(*args, **kwargs) as container:
            # A demuxer's duration is merely metadata; the frames remain real FLAC.
            yield SimpleNamespace(
                duration=999 * av.time_base,
                streams=container.streams,
                format=container.format,
                decode=container.decode,
            )

    monkeypatch.setattr(av, "open", misleading_duration)

    if route == "provider":
        assert prepare_transcription_audio(exact, {}, _FLAC_POLICY).audio == exact
        with pytest.raises(SpeechInputError) as error:
            prepare_transcription_audio(over, {}, _FLAC_POLICY)
    else:
        assert (
            sum(
                frame.samples
                for frame in iter_decoded_frames(exact, sample_rate=_RATE, sample_format="s16")
            )
            == 1600
        )
        with pytest.raises(SpeechInputError) as error:
            list(iter_decoded_frames(over, sample_rate=_RATE, sample_format="s16"))

    assert error.value.too_large


@pytest.mark.parametrize("route", ["local", "provider"])
def test_decoded_frame_limit_rejects_compressed_input_before_delivery(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    audio = _flac(1600)
    with av.open(io.BytesIO(audio), mode="r") as container:
        frame = next(container.decode(audio=0))
        allocation = sum(plane.buffer_size for plane in frame.planes)
    monkeypatch.setattr(speech_input, "_MAX_FRAME_BYTES", allocation - 1)

    with pytest.raises(SpeechInputError) as error:
        if route == "provider":
            prepare_transcription_audio(audio, {}, _FLAC_POLICY)
        else:
            next(iter_decoded_frames(audio, sample_rate=_RATE, sample_format="s16"))

    assert error.value.too_large


@pytest.mark.parametrize("ending", ["cancel", "close"])
def test_stopping_decoded_frames_closes_the_input_container(
    monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    audio = _flac(_RATE)
    open_container = av.open
    closed = False
    cancelled = False

    @contextmanager
    def tracked_open(*args: Any, **kwargs: Any) -> Iterator[Any]:
        nonlocal closed
        try:
            with open_container(*args, **kwargs) as container:
                yield container
        finally:
            closed = True

    def check_cancel() -> None:
        if cancelled:
            raise asyncio.CancelledError()

    monkeypatch.setattr(av, "open", tracked_open)
    frames = iter_decoded_frames(
        audio, sample_rate=_RATE, sample_format="s16", check_cancel=check_cancel
    )
    try:
        assert next(frames).samples > 0
        assert not closed
        if ending == "cancel":
            cancelled = True
            with pytest.raises(asyncio.CancelledError):
                next(frames)
        else:
            frames.close()
        assert closed
    finally:
        frames.close()
