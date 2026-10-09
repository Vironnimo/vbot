"""Transient playback of speech while its durable artifact is generated.

The SpeechService owns this store. Audio never enters Run history: accessors get
one URL and read binary frames from the speech HTTP endpoint. Each frame starts
with little-endian uint32 sample rate and byte length. Positive rates carry mono
PCM16; rate zero is terminal (empty payload on success, an error code otherwise).
Audio spools to a temporary file so slow readers retain the whole utterance.
Metadata stays on the owning Event Loop; file I/O uses its own bounded pool,
never the inference or decoder workers that await these playback callbacks.
"""

from __future__ import annotations

import asyncio
import struct
import tempfile
from collections.abc import AsyncGenerator, Callable
from time import monotonic
from typing import BinaryIO, cast

from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool, finish_despite_cancel

PLAYBACK_MEDIA_TYPE = "application/vnd.vbot.pcm-stream"
_FRAME_BYTES = 32_768
_MAX_PLAYBACKS = 16
_RETENTION_SECONDS = 60.0
_PLAYBACK_WORKERS = BoundedWorkerPool(name="speech-playback", max_workers=2)
_LOGGER = get_logger("speech")


def _terminal_frame(error: str = "") -> bytes:
    payload = error.encode("ascii")
    return struct.pack("<II", 0, len(payload)) + payload


class SpeechPlayback:
    """One synthesis' disk spool, read independently in bounded frames."""

    def __init__(
        self,
        identifier: str,
        *,
        clock: Callable[[], float],
        finished: Callable[[SpeechPlayback, str], None],
        idle: Callable[[SpeechPlayback], None],
    ) -> None:
        self.id = identifier
        self._clock = clock
        self._finished = finished
        self._idle = idle
        self._file: BinaryIO | None = None
        self._size = 0
        self._io = asyncio.Lock()
        self._readers = 0
        self._retired = False
        self._stopped = False
        self._changed = asyncio.Event()
        self._terminal: bytes | None = None
        self.finished_at: float | None = None

    @property
    def url(self) -> str:
        return f"/api/speech/playback/{self.id}"

    async def append(self, audio: bytes, sample_rate_hz: int) -> None:
        if self._terminal is not None:
            return
        if not 8_000 <= sample_rate_hz <= 192_000 or len(audio) % 2:
            raise ValueError("Playback needs mono PCM16 with a valid sample rate")
        if not audio:
            return
        async with self._io:
            if self._terminal is not None:
                return
            try:
                size = await _PLAYBACK_WORKERS.run(self._write, audio, sample_rate_hz, self._size)
            except OSError as error:
                self._unavailable(error)
                return
            if self._terminal is None:
                self._size = size
        self._changed.set()

    def _write(self, audio: bytes, sample_rate_hz: int, offset: int) -> int:
        if self._file is None:
            # TemporaryFile deletes on close, including process exit. It never
            # becomes a durable artifact or needs a directory sweep after a crash.
            self._file = cast(
                BinaryIO,
                tempfile.TemporaryFile(  # noqa: SIM115 - _dispose owns the reader lifetime
                    mode="w+b", prefix="vbot-speech-"
                ),
            )
        self._file.seek(offset)
        view = memoryview(audio)
        for start in range(0, len(view), _FRAME_BYTES):
            payload = view[start : start + _FRAME_BYTES]
            self._file.write(struct.pack("<II", sample_rate_hz, len(payload)))
            self._file.write(payload)
        self._file.flush()
        return self._file.tell()

    def _read(self, offset: int) -> bytes:
        assert self._file is not None
        self._file.seek(offset)
        header = self._file.read(8)
        if len(header) != 8:
            raise OSError("Speech playback spool has an incomplete header")
        _rate, length = struct.unpack("<II", header)
        if length > _FRAME_BYTES:
            raise OSError("Speech playback spool has an invalid frame")
        payload = self._file.read(length)
        if len(payload) != length:
            raise OSError("Speech playback spool has an incomplete frame")
        return header + payload

    def _close_file(self) -> None:
        if self._file is not None:
            file = self._file
            self._file = None
            file.close()

    async def _dispose(self) -> None:
        async with self._io:
            await _PLAYBACK_WORKERS.run(self._close_file)

    def _unavailable(self, error: OSError) -> None:
        _LOGGER.warning("Speech playback spool unavailable (error_type=%s)", type(error).__name__)
        self.finish("unavailable")

    def finish(self, error: str = "") -> None:
        if self._terminal is not None and (not error or self._stopped):
            return
        if error not in {"", "failed", "cancelled", "unavailable"}:
            raise ValueError("Unknown speech playback outcome")
        self._terminal = _terminal_frame(error)
        self._stopped = bool(error)
        self.finished_at = self._clock()
        self._changed.set()
        self._finished(self, error)

    def _retire(self, *, stop: bool = False) -> None:
        self._retired = True
        if stop:
            self._stopped = True
            self._terminal = _terminal_frame("cancelled")
            self._changed.set()
        if self._stopped or not self._readers:
            self._idle(self)

    async def frames(self) -> AsyncGenerator[bytes]:
        if self._retired:
            if self._stopped:
                assert self._terminal is not None
                yield self._terminal
            else:
                yield _terminal_frame("unavailable")
            return
        self._readers += 1
        cursor = 0
        try:
            while True:
                if self._stopped:
                    assert self._terminal is not None
                    yield self._terminal
                    return
                if cursor < self._size:
                    frame: bytes | None = None
                    async with self._io:
                        if self._stopped:
                            continue
                        try:
                            frame = await _PLAYBACK_WORKERS.run(self._read, cursor)
                        except OSError as error:
                            self._unavailable(error)
                    if self._stopped:
                        continue
                    assert frame is not None
                    cursor += len(frame)
                    yield frame
                elif self._terminal is not None:
                    yield self._terminal
                    return
                else:
                    self._changed.clear()
                    await self._changed.wait()
        finally:
            self._readers -= 1
            if self._retired and not self._readers:
                self._idle(self)


class SpeechPlaybackStore:
    """Service-local spools; readers never pace synthesis or retain all PCM in RAM."""

    def __init__(
        self,
        *,
        max_playbacks: int = _MAX_PLAYBACKS,
        retention_seconds: float = _RETENTION_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._max_playbacks = max_playbacks
        self._retention_seconds = retention_seconds
        self._clock = clock
        self._playbacks: dict[str, SpeechPlayback] = {}
        self._resources: set[SpeechPlayback] = set()
        self._disposing: dict[SpeechPlayback, asyncio.Task[None]] = {}
        self._expiry: dict[str, asyncio.TimerHandle] = {}
        self._closed = False

    def _prune(self) -> None:
        now = self._clock()
        for playback in list(self._playbacks.values()):
            if (
                playback.finished_at is not None
                and now - playback.finished_at >= self._retention_seconds
            ):
                self._expire(playback)

    def _finished(self, playback: SpeechPlayback, error: str) -> None:
        if error:
            self._expire(playback)
        else:
            self._expiry[playback.id] = asyncio.get_running_loop().call_later(
                self._retention_seconds, self._expire, playback
            )

    def _expire(self, playback: SpeechPlayback) -> None:
        self._playbacks.pop(playback.id, None)
        timer = self._expiry.pop(playback.id, None)
        if timer is not None:
            timer.cancel()
        playback._retire()

    def _idle(self, playback: SpeechPlayback) -> None:
        if playback in self._disposing or playback not in self._resources:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous service shutdown outside its Event Loop has no work
            # left to race; file close is allowed to block this calling thread.
            playback._close_file()
            self._resources.discard(playback)
        else:
            self._disposing[playback] = loop.create_task(
                self._dispose(playback), name=f"speech-playback-close:{playback.id}"
            )

    async def _dispose(self, playback: SpeechPlayback) -> None:
        try:
            await finish_despite_cancel(playback._dispose())
        except OSError as error:
            _LOGGER.warning("Speech playback cleanup failed (error_type=%s)", type(error).__name__)
        finally:
            self._resources.discard(playback)
            self._disposing.pop(playback, None)

    def create(self) -> SpeechPlayback | None:
        self._prune()
        if self._closed or len(self._resources) >= self._max_playbacks:
            return None
        identifier = new_id("spk", claim=lambda candidate: candidate not in self._playbacks)
        playback = SpeechPlayback(
            identifier, clock=self._clock, finished=self._finished, idle=self._idle
        )
        self._playbacks[identifier] = playback
        self._resources.add(playback)
        return playback

    def get(self, identifier: str) -> SpeechPlayback | None:
        self._prune()
        return self._playbacks.get(identifier)

    def close(self) -> None:
        self._closed = True
        for timer in self._expiry.values():
            timer.cancel()
        self._expiry.clear()
        for playback in tuple(self._resources):
            playback._retire(stop=True)
        self._playbacks.clear()

    async def aclose(self) -> None:
        self.close()
        await finish_despite_cancel(
            asyncio.gather(*self._disposing.values(), return_exceptions=True)
        )
