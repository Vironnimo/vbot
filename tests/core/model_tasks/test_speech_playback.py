"""Transient speech resources: early delivery, disk replay and cleanup."""

from __future__ import annotations

import asyncio
import struct
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.model_tasks import speech_playback
from core.model_tasks.speech_playback import SpeechPlaybackStore


@pytest.mark.asyncio
async def test_listeners_receive_audio_before_completion_and_resume_independently() -> None:
    store = SpeechPlaybackStore()
    try:
        playback = store.create()
        assert playback is not None
        first, second = playback.frames(), playback.frames()
        waiting = asyncio.create_task(anext(first))
        await asyncio.sleep(0)
        assert not waiting.done()
        await playback.append(b"\x01\x00", 24_000)
        expected = struct.pack("<II", 24_000, 2) + b"\x01\x00"
        assert await waiting == expected
        assert await anext(second) == expected
        await playback.append(b"\x02\x00", 48_000)
        playback.finish()
        rest = [struct.pack("<II", 48_000, 2) + b"\x02\x00", bytes(8)]
        assert [frame async for frame in first] == rest
        assert [frame async for frame in second] == rest
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_long_fast_synthesis_preserves_every_frame_for_slow_and_late_listeners() -> None:
    store = SpeechPlaybackStore()
    try:
        playback = store.create()
        assert playback is not None
        slow = playback.frames()
        await playback.append(bytes(2), 24_000)
        assert (await anext(slow))[8:] == bytes(2)
        # More than a minute at 24 kHz, arriving before the first listener plays
        # its second frame. The former 2 MiB replay window lost the beginning.
        payload = bytes(3 * 1024 * 1024)
        await playback.append(payload, 24_000)
        playback.finish()
        assert store.get(playback.id) is playback
        for frames, expected in ((slow, len(payload)), (playback.frames(), len(payload) + 2)):
            received = 0
            terminal = False
            async for frame in frames:
                rate, length = struct.unpack("<II", frame[:8])
                assert length <= 32_768
                assert len(frame) == length + 8
                if rate:
                    assert rate == 24_000 and not terminal
                    received += length
                else:
                    assert frame == bytes(8)
                    terminal = True
            assert received == expected and terminal
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_resource_limits_expiry_and_shutdown_release_waiting_listeners() -> None:
    now = 0.0
    store = SpeechPlaybackStore(max_playbacks=2, retention_seconds=10, clock=lambda: now)
    playback = store.create()
    assert playback is not None
    stream = playback.frames()
    await playback.append(bytes(2), 24_000)
    await anext(stream)
    await playback.append(b"\x01\x00", 24_000)
    playback.finish()
    now = 9.0
    assert store.get(playback.id) is playback
    now = 10.0
    assert store.get(playback.id) is None
    # A reader already holding the resource survives lookup expiry.
    assert (await anext(stream))[8:] == b"\x01\x00"
    assert await anext(stream) == bytes(8)
    active = store.create()
    assert active is not None
    assert store.create() is None  # The expired reader still owns its open spool.
    await stream.aclose()
    active_stream = active.frames()
    waiting = asyncio.create_task(anext(active_stream))
    await asyncio.sleep(0)
    store.close()
    assert (await waiting)[8:] == b"cancelled"
    assert store.get(active.id) is None
    assert store.create() is None
    await active_stream.aclose()
    await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancelled", "expired", "shutdown"])
async def test_spool_io_stays_off_loop_and_files_close_without_another_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    closed = asyncio.Event()
    files = []
    original_open = speech_playback.tempfile.TemporaryFile

    def open_file(**kwargs: Any) -> Any:
        assert threading.get_ident() != loop_thread
        file = original_open(dir=tmp_path, **kwargs)
        files.append(file)

        def method(name: str):
            def invoke(*args: Any) -> Any:
                assert threading.get_ident() != loop_thread
                result = getattr(file, name)(*args)
                if name == "close":
                    loop.call_soon_threadsafe(closed.set)
                return result

            return invoke

        return SimpleNamespace(
            **{name: method(name) for name in ("seek", "read", "write", "tell", "flush", "close")}
        )

    monkeypatch.setattr(speech_playback.tempfile, "TemporaryFile", open_file)
    store = SpeechPlaybackStore(retention_seconds=0 if ending == "expired" else 10)
    try:
        playback = store.create()
        assert playback is not None and files == []
        await playback.append(bytes(2), 24_000)
        stream = playback.frames()
        assert (await anext(stream))[8:] == bytes(2)
        await stream.aclose()
        if ending == "cancelled":
            playback.finish("cancelled")
        elif ending == "expired":
            playback.finish()
        else:
            store.close()
        async with asyncio.timeout(1):
            await closed.wait()
        assert len(files) == 1 and files[0].closed
        assert list(tmp_path.iterdir()) == []
    finally:
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["create", "read"])
async def test_spool_failure_stops_playback_without_failing_synthesis(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("Test spool failure")

    store = SpeechPlaybackStore()
    try:
        playback = store.create()
        assert playback is not None
        if failure == "create":
            monkeypatch.setattr(speech_playback.tempfile, "TemporaryFile", unavailable)
        await playback.append(bytes(2), 24_000)
        playback.finish()
        if failure == "read":
            monkeypatch.setattr(playback, "_read", unavailable)
        assert [frame async for frame in playback.frames()] == [
            struct.pack("<II", 0, 11) + b"unavailable"
        ]
        assert store.get(playback.id) is None
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_cancel_and_shutdown_settle_a_running_spool_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    writing = asyncio.Event()
    release = threading.Event()
    files = []
    original_open = speech_playback.tempfile.TemporaryFile

    def blocked_open(**kwargs: Any) -> Any:
        loop.call_soon_threadsafe(writing.set)
        release.wait()
        file = original_open(**kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(speech_playback.tempfile, "TemporaryFile", blocked_open)
    store = SpeechPlaybackStore()
    playback = store.create()
    assert playback is not None
    append = asyncio.create_task(playback.append(bytes(2), 24_000))
    try:
        async with asyncio.timeout(1):
            await writing.wait()
            append.cancel()
            store.close()
            assert not append.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await append
            await store.aclose()
        assert len(files) == 1 and files[0].closed
        assert [frame async for frame in playback.frames()] == [
            struct.pack("<II", 0, 9) + b"cancelled"
        ]
    finally:
        release.set()
        await asyncio.gather(append, return_exceptions=True)
        await store.aclose()
