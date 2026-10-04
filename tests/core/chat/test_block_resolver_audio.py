"""ContentBlockResolver audio: native delivery, speech-to-text degradation and path notes."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.attachments import AttachmentStore
from core.chat.block_resolver import ContentBlockResolver
from core.model_tasks import SpeechExecutionError
from tests.core.chat.block_resolver_test_support import (
    CURRENT,
    EARLIER,
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
    (
        "filename",
        "message_id",
        "modalities",
        "wire",
        "stt",
        "cached",
        "expected",
        "called",
        "stored",
    ),
    [
        (
            "clip.wav",
            CURRENT,
            TEXT_IMAGE_AUDIO,
            IMAGE_AUDIO_WIRE,
            "unused",
            None,
            [NATIVE, "[Audio: clip.wav (audio/wav) — Path: {path}]"],
            False,
            None,
        ),
        (
            "voice.ogg",
            CURRENT,
            TEXT_IMAGE,
            IMAGE_AUDIO_WIRE,
            "hallo welt",
            None,
            [
                _transcript("voice.ogg", "audio/ogg", "hallo welt"),
                "[Audio: voice.ogg (audio/ogg) — Path: {path}]",
            ],
            True,
            "hallo welt",
        ),
        # The model accepts audio, but the wire cannot carry it: never emit unencodable audio.
        (
            "clip.wav",
            CURRENT,
            TEXT_IMAGE_AUDIO,
            IMAGE_WIRE,
            "from stt",
            None,
            [
                _transcript("clip.wav", "audio/wav", "from stt"),
                "[Audio: clip.wav (audio/wav) — Path: {path}]",
            ],
            True,
            "from stt",
        ),
        (
            "voice.ogg",
            CURRENT,
            TEXT_IMAGE,
            IMAGE_AUDIO_WIRE,
            None,
            None,
            ["[Audio: voice.ogg (audio/ogg) — " + NO_STT + "Path: {path}]"],
            False,
            None,
        ),
        (
            "voice.ogg",
            CURRENT,
            TEXT_IMAGE,
            IMAGE_AUDIO_WIRE,
            FAILED,
            None,
            ["[Audio: voice.ogg (audio/ogg) — " + STT_FAILED + "Path: {path}]"],
            True,
            None,
        ),
        (
            "voice.ogg",
            CURRENT,
            TEXT_IMAGE,
            IMAGE_AUDIO_WIRE,
            "   ",
            None,
            ["[Audio: voice.ogg (audio/ogg) — " + STT_FAILED + "Path: {path}]"],
            True,
            None,
        ),
        # A cached transcription wins over native delivery and a new speech-to-text call.
        (
            "clip.wav",
            CURRENT,
            TEXT_IMAGE_AUDIO,
            IMAGE_AUDIO_WIRE,
            "unused",
            "cached words",
            [
                _transcript("clip.wav", "audio/wav", "cached words"),
                "[Audio: clip.wav (audio/wav) — Path: {path}]",
            ],
            False,
            "cached words",
        ),
        (
            "voice.ogg",
            EARLIER,
            TEXT_IMAGE,
            IMAGE_AUDIO_WIRE,
            "unused",
            "what was said",
            [
                _transcript("voice.ogg", "audio/ogg", "what was said"),
                "[Audio: voice.ogg (audio/ogg) — Path: {path}]",
            ],
            False,
            "what was said",
        ),
        (
            "clip.wav",
            EARLIER,
            TEXT_IMAGE_AUDIO,
            IMAGE_AUDIO_WIRE,
            "unused",
            None,
            [NATIVE, "[Audio: clip.wav (audio/wav) — Path: {path}]"],
            False,
            None,
        ),
        # Speech-to-text runs only on the audio's own turn, never again per request.
        (
            "voice.ogg",
            EARLIER,
            TEXT_IMAGE,
            IMAGE_AUDIO_WIRE,
            "unused",
            None,
            ["[Audio: voice.ogg (audio/ogg) — " + STT_FAILED + "Path: {path}]"],
            False,
            None,
        ),
    ],
    ids=[
        "native",
        "transcribed-without-audio-modality",
        "transcribed-on-a-wire-without-audio",
        "no-speech-to-text",
        "speech-to-text-fails",
        "speech-to-text-empty",
        "cached-transcription",
        "earlier-with-transcription",
        "earlier-native",
        "earlier-not-transcribed-again",
    ],
)
def test_audio_is_native_transcribed_or_a_path_note(
    tmp_path: Path,
    filename: str,
    message_id: str,
    modalities: frozenset[str],
    wire: frozenset[str],
    stt: str | None,
    cached: str | None,
    expected: list[str],
    called: bool,
    stored: str | None,
) -> None:
    # Missing or failing speech-to-text never aborts the Run: the Agent keeps the path.
    store = AttachmentStore(tmp_path)
    payload = WAV_BYTES if filename.endswith(".wav") else OGG_BYTES
    record = store.store(filename, payload)
    if cached is not None:
        store.set_transcription(record.id, cached)
    transcriber = _StubTranscriber(stt) if stt is not None else None
    resolver = ContentBlockResolver(store, transcriber=transcriber)

    resolved = resolve(
        resolver,
        [attachment_message(record, message_id=message_id)],
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
        assert transcriber.calls == ([(filename, record.media_type)] if called else [])
    # A new transcription is cached on the attachment for later turns.
    assert store.get(record.id).transcription == stored


def test_current_audio_with_unreadable_metadata_degrades_to_an_unavailable_note(
    tmp_path: Path,
) -> None:
    store = AttachmentStore(tmp_path)
    record = store.store("gone.wav", WAV_BYTES)
    store.delete(record.id)

    resolved = resolve(
        ContentBlockResolver(store),
        [attachment_message(record)],
        input_modalities=TEXT_IMAGE_AUDIO,
    )

    assert resolved[0]["content"] == [
        {"type": "text", "text": "[Audio: gone.wav (audio/wav) — file no longer available]"}
    ]
