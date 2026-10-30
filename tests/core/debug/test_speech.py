"""Speech diagnostics preserve exact uploads under live Debug and retention policy."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.debug import SpeechDebugDisabledError, SpeechDebugStore
from core.storage.layout import DataDirectoryLayout


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, RuntimeError("inference sentinel")])
async def test_original_upload_and_outcome_survive_reopening(
    tmp_path: Path, error: Exception | None
) -> None:
    settings = {"enabled": True, "speech_recording_limit": 10}
    store = SpeechDebugStore(tmp_path, lambda: settings)
    audio = b"exact original upload\x00\xff"
    capture = store.begin(audio, "audio/webm;codecs=opus")
    assert capture is not None
    capture.finish("Licht an." if error is None else None, error)
    reopened = SpeechDebugStore(tmp_path, lambda: settings)
    records = await reopened.list_async()
    assert len(records) == 1
    record = records[0]
    assert record["text"] == ("Licht an." if error is None else None)
    assert record["error"] == (
        None if error is None else {"type": "RuntimeError", "message": str(error)}
    )
    assert await reopened.audio_async(record["recording_id"]) == (audio, "audio/webm")


@pytest.mark.asyncio
async def test_debug_must_be_enabled_at_admission_and_publication_and_clear_works_while_off(
    tmp_path: Path,
) -> None:
    settings: dict[str, Any] = {"enabled": False}
    store = SpeechDebugStore(tmp_path, lambda: settings)
    assert store.begin(b"not recorded", "audio/wav") is None
    settings["enabled"] = True
    capture = store.begin(b"in flight", "audio/wav")
    assert capture is not None
    settings["enabled"] = False
    capture.finish("too late", None)
    await store.prune_async()  # drain the earlier publication while capture is off
    with pytest.raises(SpeechDebugDisabledError):
        await store.list_async()
    settings["enabled"] = True
    assert await store.list_async() == []
    capture = store.begin(b"retained", "audio/wav")
    assert capture is not None
    capture.finish("retained text", None)
    record = (await store.list_async())[0]
    settings["enabled"] = False
    with pytest.raises(SpeechDebugDisabledError):
        await store.audio_async(record["recording_id"])
    await store.clear_async()
    settings["enabled"] = True
    assert await store.list_async() == []


@pytest.mark.asyncio
async def test_retention_removes_oldest_audio_and_results_and_applies_a_smaller_limit(
    tmp_path: Path,
) -> None:
    settings = {"enabled": True, "speech_recording_limit": 2}
    now = [datetime(2026, 10, 30, tzinfo=UTC)]
    store = SpeechDebugStore(tmp_path, lambda: settings, clock=lambda: now[0])
    for index in range(3):
        now[0] = now[0].replace(second=index)
        capture = store.begin(bytes([index]), "audio/wav")
        assert capture is not None
        capture.finish(str(index), None)
        await store.list_async()
    assert [record["text"] for record in await store.list_async()] == ["2", "1"]
    settings["speech_recording_limit"] = 1
    await store.prune_async()
    records = await store.list_async()
    assert [record["text"] for record in records] == ["2"]
    directory = DataDirectoryLayout(tmp_path).debug / "speech"
    assert len(list(directory.iterdir())) == 2
    for recording_id in ("../outside", records[0]["recording_id"].upper()):
        with pytest.raises(ValueError):
            await store.audio_async(recording_id)
    with pytest.raises(FileNotFoundError):
        await store.audio_async("stt_missing")


@pytest.mark.asyncio
async def test_unreadable_metadata_cannot_authorize_retention_cleanup(
    tmp_path: Path, deny_access: Any
) -> None:
    settings = {"enabled": True, "speech_recording_limit": 1}
    store = SpeechDebugStore(tmp_path, lambda: settings)
    capture = store.begin(b"keep this audio", "audio/wav")
    assert capture is not None
    capture.finish("first", None)
    recording_id = (await store.list_async())[0]["recording_id"]
    directory = DataDirectoryLayout(tmp_path).debug / "speech"
    deny_access(directory / f"{recording_id}.json")
    capture = store.begin(b"new audio", "audio/wav")
    assert capture is not None
    capture.finish("second", None)
    with pytest.raises(PermissionError):
        await store.list_async()
    assert (directory / f"{recording_id}.audio").read_bytes() == b"keep this audio"
