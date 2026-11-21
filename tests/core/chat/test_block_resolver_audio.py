"""ContentBlockResolver audio: speech-to-text before persistence, then native or text delivery."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.attachments import AttachmentStore
from core.chat.block_resolver import AttachmentResolveError, ContentBlockResolver
from core.chat.content_blocks import MediaBlock, TextBlock
from core.model_tasks import SpeechExecutionError
from core.model_tasks.speech_types import SpeechBusyError
from tests.core.chat.block_resolver_test_support import (
    IMAGE_AUDIO_WIRE,
    IMAGE_WIRE,
    TEXT_IMAGE,
    TEXT_IMAGE_AUDIO,
    attachment_message,
    path_note,
    resolve,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt wav-payload"
OGG_BYTES = b"OggS\x00\x02voice-payload"
NATIVE = "<native audio>"
FAILED = "<speech-to-text fails>"
NO_STT = (
    "this model cannot accept audio and no speech-to-text service is available, "
    "so only the stored file path is provided — "
)
STT_FAILED = (
    "speech-to-text could not transcribe this audio, so only the stored file path is provided — "
)


class _StubTranscriber:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, str]] = []

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> object:
        self.calls.append((filename, media_type))
        if self.text == FAILED:
            raise SpeechExecutionError("provider unavailable")
        return SimpleNamespace(text=self.text)


def _transcript(filename: str, media_type: str, text: str) -> str:
    return (
        f"[Audio attachment {filename} ({media_type}) — automatic transcription, "
        f"may contain recognition errors]:\n{text}"
    )


@pytest.mark.parametrize(
    ("stt", "cached", "carried", "expected", "called"),
    [
        ("hallo welt", None, None, "hallo welt", True),
        (None, None, None, None, False),
        (FAILED, None, None, None, True),
        ("   ", None, None, None, True),
        # An attachment transcribed before reuses its text without a new call.
        ("unused", "cached words", None, "cached words", False),
        # A block that already carries a transcription keeps it.
        ("unused", None, "edited words", "edited words", False),
    ],
    ids=[
        "transcribed",
        "no-speech-to-text",
        "speech-to-text-fails",
        "speech-to-text-empty",
        "cached-on-the-attachment",
        "carried-by-the-block",
    ],
)
def test_incoming_audio_blocks_carry_their_transcription(
    tmp_path: Path,
    stt: str | None,
    cached: str | None,
    carried: str | None,
    expected: str | None,
    called: bool,
) -> None:
    # Missing or failing speech-to-text never refuses the input: the block stays as it was.
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", OGG_BYTES)
    image = store.store("photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    if cached is not None:
        store.set_transcription(record.id, cached)
    transcriber = _StubTranscriber(stt) if stt is not None else None
    resolver = ContentBlockResolver(store, transcriber=transcriber)
    text = TextBlock("text", "Listen:")
    picture = MediaBlock("media", image.id, image.filename, image.media_type)
    audio = MediaBlock(
        "media", record.id, record.filename, record.media_type, transcription=carried
    )

    alias = replace(audio, filename="voice-again.ogg")
    content = asyncio.run(resolver.transcribe_audio([audio, text, picture, alias]))

    assert content == [
        replace(audio, transcription=expected),
        text,
        picture,
        replace(alias, transcription=expected),
    ]
    assert asyncio.run(resolver.transcribe_audio("plain text")) == "plain text"
    if transcriber is not None:
        assert transcriber.calls == ([(record.filename, record.media_type)] if called else [])
    # A new transcription is cached on the attachment, so a re-sent block reuses it.
    assert store.get(record.id).transcription == (cached or (expected if called else None))


@pytest.mark.asyncio
async def test_audio_batch_transcribes_at_most_two_attachments_and_keeps_order(
    tmp_path: Path,
) -> None:
    started: asyncio.Queue[str] = asyncio.Queue()
    gates = {name: asyncio.Event() for name in ("a.ogg", "b.ogg", "c.ogg", "d.ogg")}
    active = peak = 0

    class Transcriber:
        has_transcription_capacity = True

        async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> object:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            started.put_nowait(filename)
            try:
                await gates[filename].wait()
                if filename == "c.ogg":
                    raise SpeechExecutionError("provider unavailable")
                return SimpleNamespace(text=filename)
            finally:
                active -= 1

    store = AttachmentStore(tmp_path)
    blocks: list[MediaBlock] = []
    for name in gates:
        record = store.store(name, OGG_BYTES)
        blocks.append(MediaBlock("media", record.id, name, record.media_type))
    resolver = ContentBlockResolver(store, transcriber=Transcriber())
    batch = asyncio.create_task(resolver.transcribe_audio([*blocks, blocks[2]]))
    try:
        assert {await started.get(), await started.get()} == {"a.ogg", "b.ogg"}
        gates["b.ogg"].set()
        assert await started.get() == "c.ogg"
        gates["c.ogg"].set()
        assert await started.get() == "d.ogg"
    finally:
        for gate in gates.values():
            gate.set()
        content = await batch
    assert peak == 2
    assert started.empty()
    assert content == [
        replace(blocks[0], transcription="a.ogg"),
        replace(blocks[1], transcription="b.ogg"),
        blocks[2],
        replace(blocks[3], transcription="d.ogg"),
        blocks[2],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("occupied", [3, 4], ids=["one-place-left", "full"])
async def test_audio_batch_shares_remaining_speech_capacity(tmp_path: Path, occupied: int) -> None:
    full = asyncio.Event()
    attempts: list[str] = []

    class Transcriber:
        @property
        def has_transcription_capacity(self) -> bool:
            if occupied == 4:
                full.set()
                return False
            return True

        async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> object:
            nonlocal occupied
            attempts.append(filename)
            if not self.has_transcription_capacity:
                raise SpeechBusyError("full")
            occupied += 1
            try:
                await full.wait()
                return SimpleNamespace(text=filename)
            finally:
                occupied -= 1

    store = AttachmentStore(tmp_path)
    records = [store.store(f"{n}.ogg", OGG_BYTES) for n in range(3)]
    blocks = [MediaBlock("media", r.id, r.filename, r.media_type) for r in records]
    resolver = ContentBlockResolver(store, transcriber=Transcriber())
    content = await resolver.transcribe_audio([*blocks])

    assert sorted(attempts) == [r.filename for r in records]
    assert content == [
        replace(block, transcription=block.filename if occupied == 3 else None) for block in blocks
    ]


@pytest.mark.parametrize(
    ("filename", "modalities", "wire", "transcription", "stt", "expected"),
    [
        (
            "clip.wav",
            TEXT_IMAGE_AUDIO,
            IMAGE_AUDIO_WIRE,
            None,
            True,
            [NATIVE, "[Audio: clip.wav (audio/wav) — Path: {path}]"],
        ),
        # The transcription the message carries wins over native delivery.
        (
            "clip.wav",
            TEXT_IMAGE_AUDIO,
            IMAGE_AUDIO_WIRE,
            "what was said",
            True,
            [
                _transcript("clip.wav", "audio/wav", "what was said"),
                "[Audio: clip.wav (audio/wav) — Path: {path}]",
            ],
        ),
        (
            "voice.ogg",
            TEXT_IMAGE,
            IMAGE_AUDIO_WIRE,
            "hallo welt",
            True,
            [
                _transcript("voice.ogg", "audio/ogg", "hallo welt"),
                "[Audio: voice.ogg (audio/ogg) — Path: {path}]",
            ],
        ),
        # The model accepts audio, but the wire cannot carry it: never emit unencodable audio.
        (
            "clip.wav",
            TEXT_IMAGE_AUDIO,
            IMAGE_WIRE,
            None,
            True,
            ["[Audio: clip.wav (audio/wav) — " + STT_FAILED + "Path: {path}]"],
        ),
        (
            "voice.ogg",
            TEXT_IMAGE,
            IMAGE_AUDIO_WIRE,
            None,
            False,
            ["[Audio: voice.ogg (audio/ogg) — " + NO_STT + "Path: {path}]"],
        ),
    ],
    ids=[
        "native",
        "transcription-wins",
        "transcription-without-audio-modality",
        "untranscribed-on-a-wire-without-audio",
        "untranscribed-without-speech-to-text",
    ],
)
def test_audio_request_renders_its_transcription_native_audio_or_a_path_note(
    tmp_path: Path,
    filename: str,
    modalities: frozenset[str],
    wire: frozenset[str],
    transcription: str | None,
    stt: bool,
    expected: list[str],
) -> None:
    # Building a request never runs speech-to-text: it ran before the message persisted.
    store = AttachmentStore(tmp_path)
    payload = WAV_BYTES if filename.endswith(".wav") else OGG_BYTES
    record = store.store(filename, payload)
    transcriber = _StubTranscriber("unused") if stt else None
    resolver = ContentBlockResolver(store, transcriber=transcriber)
    fields = {"transcription": transcription} if transcription is not None else {}

    resolved = resolve(
        resolver,
        [attachment_message(record, **fields)],
        input_modalities=modalities,
        wire_media_types=wire,
    )

    native = {
        "type": "media",
        "base64": base64.b64encode(payload).decode("ascii"),
        "media_type": record.media_type,
    }
    assert resolved[0]["content"] == [
        native if text == NATIVE else path_note(record, text) for text in expected
    ]
    if transcriber is not None:
        assert transcriber.calls == []


@pytest.mark.parametrize("unreadable", [False, True], ids=["missing", "unreadable"])
def test_audio_transcription_is_best_effort_but_request_rejects_io_failure(
    tmp_path: Path,
    unreadable: bool,
    deny_access,
) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("gone.wav", WAV_BYTES)
    if unreadable:
        deny_access(Path(record.file_path).with_suffix(".json"))
    else:
        store.delete(record.id)
    resolver = ContentBlockResolver(store, transcriber=_StubTranscriber("unused"))
    audio = MediaBlock("media", record.id, record.filename, record.media_type)

    assert asyncio.run(resolver.transcribe_audio([audio])) == [audio]
    if unreadable:
        with pytest.raises(AttachmentResolveError):
            resolve(resolver, [attachment_message(record)], input_modalities=TEXT_IMAGE_AUDIO)
        return
    resolved = resolve(resolver, [attachment_message(record)], input_modalities=TEXT_IMAGE_AUDIO)

    assert resolved[0]["content"] == [
        {"type": "text", "text": "[Audio: gone.wav (audio/wav) — file no longer available]"}
    ]
