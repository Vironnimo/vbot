"""Incremental speech playback and decoder cleanup without network requests."""

from __future__ import annotations

import asyncio
import builtins
import io
import threading
import wave
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import av
import numpy as np
import pytest

from core.model_tasks import speech_audio
from core.model_tasks.speech_types import SpeechAudioChunk


@pytest.fixture(scope="module")
def recordings() -> dict[str, bytes]:
    samples = (8000 * np.sin(np.arange(8 * 24_000) * 2 * np.pi * 440 / 24_000)).astype("<i2")
    output = io.BytesIO()
    with av.open(output, "w", format="mp3") as container:
        stream = container.add_stream("libmp3lame", rate=24_000)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 24_000
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return {
        "pcm": samples.tobytes(),
        "wav": speech_audio.pcm_to_wav(samples.tobytes(), 24_000),
        "mp3": output.getvalue(),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("audio_format", ["wav", "mp3", "pcm"])
async def test_playback_starts_before_remaining_input_and_retains_original(
    recordings, audio_format, monkeypatch
):
    imported = builtins.__import__

    def without_numpy(name, *args, **kwargs):
        if name == "numpy" or name.startswith("numpy."):
            raise ModuleNotFoundError("NumPy is not part of the server speech dependencies")
        return imported(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_numpy)
    recording = recordings[audio_format]
    heard, remainder = asyncio.Event(), asyncio.Event()
    received: list[SpeechAudioChunk] = []

    async def chunks() -> AsyncIterator[bytes]:
        yield recording[: len(recording) // 2]
        await remainder.wait()
        yield recording[len(recording) // 2 :]

    async def on_audio(chunk: SpeechAudioChunk) -> None:
        received.append(chunk)
        heard.set()

    request = asyncio.create_task(
        speech_audio.decode_speech_audio(
            chunks(), on_audio, audio_format=audio_format, pcm_sample_rate=24_000
        )
    )
    try:
        await asyncio.wait_for(heard.wait(), 5)
        assert not request.done()
        remainder.set()
        assert await request == recording
        assert all(
            chunk.sample_rate_hz == 24_000 and len(chunk.audio) % 2 == 0 for chunk in received
        )
        samples = b"".join(chunk.audio for chunk in received)
        assert any(samples)
        if audio_format != "mp3":
            assert samples == recordings["pcm"]
    finally:
        remainder.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending", ["cancellation", "callback_cancellation", "transport", "consumer"]
)
async def test_interrupted_stream_waits_for_decoder_cleanup(recordings, monkeypatch, ending):
    heard, source_closed = asyncio.Event(), asyncio.Event()
    callback_cleaning, release_callback = asyncio.Event(), asyncio.Event()
    decoder_stopped, release_cleanup, decoder_reaped = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    decode = speech_audio._decode
    failure = RuntimeError("test stream failure")

    def decode_with_cleanup(*arguments: Any) -> None:
        try:
            decode(*arguments)
        finally:
            decoder_stopped.set()
            assert release_cleanup.wait(5)
            decoder_reaped.set()

    monkeypatch.setattr(speech_audio, "_decode", decode_with_cleanup)

    async def chunks() -> AsyncIterator[bytes]:
        try:
            yield recordings["wav"][: len(recordings["wav"]) // 2]
            await heard.wait()
            if ending == "transport":
                raise failure
            await asyncio.Event().wait()
        finally:
            source_closed.set()

    async def on_audio(_chunk: SpeechAudioChunk) -> None:
        heard.set()
        if ending == "consumer":
            raise failure
        if ending == "callback_cancellation":
            try:
                await asyncio.Event().wait()
            finally:
                callback_cleaning.set()
                await release_callback.wait()

    request = asyncio.create_task(
        speech_audio.decode_speech_audio(chunks(), on_audio, audio_format="wav")
    )
    try:
        await asyncio.wait_for(heard.wait(), 5)
        if ending in {"cancellation", "callback_cancellation"}:
            request.cancel()
        if ending == "callback_cancellation":
            await asyncio.wait_for(callback_cleaning.wait(), 5)
            assert not request.done() and not decoder_stopped.is_set()
            release_callback.set()
        assert await asyncio.to_thread(decoder_stopped.wait, 5)
        assert not request.done()
        release_cleanup.set()
        if ending in {"cancellation", "callback_cancellation"}:
            with pytest.raises(asyncio.CancelledError):
                await request
        else:
            with pytest.raises(RuntimeError) as error:
                await request
            assert error.value is failure
        assert decoder_reaped.is_set() and source_closed.is_set()
    finally:
        release_callback.set()
        release_cleanup.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audio_format", "audio", "sample_rate", "channels"),
    [
        ("wav", b"not a wave recording", None, 1),
        ("mp3", b"", None, 1),
        ("pcm", b"\x00" * 4097, 24_000, 1),
        ("pcm", b"\x00" * 4098, 24_000, 2),
        ("pcm", b"\x00\x01", None, 1),
        ("pcm", b"\x00\x01", 0, 1),
        ("pcm", b"\x00\x01", 24_000, 0),
    ],
    ids=[
        "invalid_wav",
        "empty_mp3",
        "odd_pcm",
        "partial_stereo",
        "unknown_rate",
        "zero_rate",
        "zero_channels",
    ],
)
async def test_invalid_audio_and_pcm_metadata_are_rejected(
    audio_format, audio, sample_rate, channels
):
    async def chunks() -> AsyncIterator[bytes]:
        yield audio

    async def on_audio(_chunk: SpeechAudioChunk) -> None:
        pass

    with pytest.raises(ValueError):
        await speech_audio.decode_speech_audio(
            chunks(),
            on_audio,
            audio_format=audio_format,
            pcm_sample_rate=sample_rate,
            pcm_channels=channels,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["encoded", "decoded"])
async def test_encoded_and_decoded_sizes_are_bounded(recordings, monkeypatch, bound):
    recording = recordings["wav" if bound == "encoded" else "mp3"]
    limit = len(recording) // 2 if bound == "encoded" else len(recording) * 2
    assert limit < len(recordings["pcm"])
    monkeypatch.setattr(speech_audio, "MAX_SPEECH_BYTES", limit)
    received = 0

    async def chunks() -> AsyncIterator[bytes]:
        yield recording

    async def on_audio(chunk: SpeechAudioChunk) -> None:
        nonlocal received
        received += len(chunk.audio)

    with pytest.raises(ValueError):
        await speech_audio.decode_speech_audio(
            chunks(), on_audio, audio_format="wav" if bound == "encoded" else "mp3"
        )
    assert received <= limit
    if bound == "decoded":
        assert received > 0


def test_pcm_container_preserves_sample_format_and_refuses_incomplete_samples():
    samples = b"\x00\x01\x00\x02" * 100
    with wave.open(io.BytesIO(speech_audio.pcm_to_wav(samples, 24_000, 2))) as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (2, 2, 24_000)
        assert audio.readframes(100) == samples
    with pytest.raises(ValueError):
        speech_audio.pcm_to_wav(samples[:-1], 24_000, 2)


def test_parallel_streams_make_progress_when_decoders_occupy_all_executor_threads():
    async def scenario() -> None:
        # Isolate the limited executor from the suite's shared Event Loop.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
        remainder = asyncio.Event()
        heard = [asyncio.Event(), asyncio.Event()]
        audio = b"\x00\x01" * 24_000

        async def request(index: int) -> bytes:
            async def chunks() -> AsyncIterator[bytes]:
                yield audio
                await remainder.wait()
                yield audio

            async def on_audio(_chunk: SpeechAudioChunk) -> None:
                heard[index].set()

            return await speech_audio.decode_speech_audio(
                chunks(), on_audio, audio_format="pcm", pcm_sample_rate=24_000
            )

        requests = [asyncio.create_task(request(index)) for index in range(2)]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in heard)), 1)
            assert all(not task.done() for task in requests)
            remainder.set()
            assert await asyncio.gather(*requests) == [audio + audio] * 2
        finally:
            remainder.set()
            for task in requests:
                task.cancel()
            await asyncio.gather(*requests, return_exceptions=True)

    asyncio.run(scenario())
