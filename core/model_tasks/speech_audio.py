"""Incremental speech decoding and playable PCM containers.

The Provider keeps its original artifact bytes while this internal decoder feeds
PCM to playback. A bounded pipe lets PyAV demux on a worker without blocking the
Event Loop or reading the entire HTTP response before playback starts.
"""

from __future__ import annotations

import asyncio
import io
import sys
import wave
from array import array
from collections.abc import AsyncIterable, Callable
from threading import Condition, Lock
from typing import Any

from core.model_tasks.speech_types import SpeechAudioCallback, SpeechAudioChunk
from core.utils.workers import settle_before_cancelling

MAX_SPEECH_BYTES = 64 * 1024 * 1024
_PIPE_BYTES = 256 * 1024


def _validate_pcm_format(sample_rate_hz: int | None, channels: int) -> None:
    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise ValueError("Speech PCM sample rate is unknown or invalid")
    if type(channels) is not int or channels <= 0:
        raise ValueError("Speech PCM channel count is invalid")


def pcm_to_wav(audio: bytes, sample_rate_hz: int, channels: int = 1) -> bytes:
    """Wrap known signed PCM16 little-endian audio without changing its samples."""
    _validate_pcm_format(sample_rate_hz, channels)
    if not audio or len(audio) % (2 * channels):
        raise ValueError("Speech PCM contains incomplete samples")
    if len(audio) > MAX_SPEECH_BYTES:
        raise ValueError("Speech response exceeds the audio size limit")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((channels, 2, sample_rate_hz, 0, "NONE", "not compressed"))
        wav.writeframes(audio)
    return output.getvalue()


class ThreadAudioCallback:
    """Bridge a serialized decoder/engine worker to its async playback consumer."""

    def __init__(self, callback: SpeechAudioCallback) -> None:
        self._loop = asyncio.get_running_loop()
        self._callback = callback
        self._lock = Lock()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def __call__(self, chunk: SpeechAudioChunk) -> None:
        with self._lock:
            if self._closed:
                return
            future = asyncio.run_coroutine_threadsafe(self._invoke(chunk), self._loop)
        future.result()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._task is not None:
                # Cancel the coroutine itself, not its cross-thread Future: the
                # worker must wait until the callback's async cleanup finishes.
                self._loop.call_soon_threadsafe(self._task.cancel)

    async def _invoke(self, chunk: SpeechAudioChunk) -> None:
        with self._lock:
            if self._closed:
                return
            self._task = asyncio.current_task()
        try:
            await self._callback(chunk)
        finally:
            with self._lock:
                self._task = None


class _AudioPipe:
    """A bounded, non-seekable file object read by PyAV's blocking demuxer."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._writable = asyncio.Event()
        self._condition = Condition()
        self._buffer = bytearray()
        self._ended = False
        self._aborted = False

    async def write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            with self._condition:
                if self._aborted:
                    return
                if self._ended:
                    raise ValueError("Speech audio decoder closed")
                end = min(len(data), offset + _PIPE_BYTES - len(self._buffer))
                if end == offset:
                    self._writable.clear()
                else:
                    self._buffer.extend(data[offset:end])
                    self._condition.notify_all()
            if end == offset:
                # Writes must not occupy executor threads: concurrent blocking
                # readers could otherwise consume every thread before input arrives.
                await self._writable.wait()
            else:
                offset = end

    def read(self, size: int = -1) -> bytes:
        with self._condition:
            while not self._buffer and not self._ended:
                self._condition.wait()
            if self._aborted:
                return b""
            take = len(self._buffer) if size < 0 else min(size, len(self._buffer))
            result = bytes(self._buffer[:take])
            del self._buffer[:take]
            self._loop.call_soon_threadsafe(self._writable.set)
            return result

    def finish(self, *, abort: bool = False) -> None:
        with self._condition:
            self._ended = True
            self._aborted = abort
            self._condition.notify_all()
            self._loop.call_soon_threadsafe(self._writable.set)


def _decode(
    source: Any,
    callback: Callable[[SpeechAudioChunk], None],
    audio_format: str,
    pcm_sample_rate: int | None,
    pcm_channels: int,
) -> None:
    import av

    options = {"probesize": "4096", "analyzeduration": "0"}
    if audio_format == "pcm":
        _validate_pcm_format(pcm_sample_rate, pcm_channels)
        options.update(sample_rate=str(pcm_sample_rate), channels=str(pcm_channels))
    format_name = {"pcm": "s16le", "opus": "ogg"}.get(audio_format, audio_format)
    decoded_bytes = 0

    def emit(frame: Any) -> None:
        nonlocal decoded_bytes
        # Packed mono s16 needs no NumPy; PyAV planes can contain alignment
        # padding, which is not part of the audio. s16 uses the host byte order.
        audio = bytes(memoryview(frame.planes[0])[: frame.samples * 2])
        if sys.byteorder != "little":
            samples = array("h", audio)
            samples.byteswap()
            audio = samples.tobytes()
        decoded_bytes += len(audio)
        if decoded_bytes > MAX_SPEECH_BYTES:
            raise ValueError("Decoded speech exceeds the audio size limit")
        if audio:
            callback(SpeechAudioChunk(audio, frame.sample_rate))

    try:
        with av.open(source, mode="r", format=format_name, options=options) as container:
            resampler = av.AudioResampler(format="s16", layout="mono")
            for frame in container.decode(audio=0):
                for converted in resampler.resample(frame):
                    emit(converted)
            for converted in resampler.resample(None):
                emit(converted)
    except av.error.FFmpegError as error:
        raise ValueError("Speech response contains invalid audio") from error
    if not decoded_bytes:
        raise ValueError("Speech response contains no audio samples")


async def decode_speech_audio(
    chunks: AsyncIterable[bytes],
    on_audio: SpeechAudioCallback,
    *,
    audio_format: str,
    pcm_sample_rate: int | None = None,
    pcm_channels: int = 1,
) -> bytes:
    """Deliver decoded PCM as input arrives and retain the original artifact bytes."""
    if audio_format == "pcm":
        _validate_pcm_format(pcm_sample_rate, pcm_channels)
    pipe = _AudioPipe()
    callback = ThreadAudioCallback(on_audio)
    original = bytearray()

    async def feed() -> None:
        iterator = aiter(chunks)
        try:
            async for chunk in iterator:
                if len(original) + len(chunk) > MAX_SPEECH_BYTES:
                    raise ValueError("Speech response exceeds the audio size limit")
                original.extend(chunk)
                await pipe.write(chunk)
            if audio_format == "pcm" and len(original) % (2 * pcm_channels):
                raise ValueError("Speech PCM contains incomplete samples")
            pipe.finish()
        finally:
            if (close := getattr(iterator, "aclose", None)) is not None:
                await close()

    producer = asyncio.create_task(feed())
    decoder = asyncio.create_task(
        asyncio.to_thread(_decode, pipe, callback, audio_format, pcm_sample_rate, pcm_channels)
    )
    try:
        pending = {producer, decoder}
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            # Keep both tasks alive under caller cancellation: cancelling to_thread
            # would lose the decoder before its blocking work actually finishes.
            for task in (producer, decoder):
                if task in done:
                    task.result()
    finally:
        pipe.finish(abort=True)
        callback.close()
        producer.cancel()
        await settle_before_cancelling(asyncio.gather(producer, decoder, return_exceptions=True))
    return bytes(original)
