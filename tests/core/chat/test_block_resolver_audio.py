"""Tests for block resolver audio."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.attachments import AttachmentStore
from core.chat.block_resolver import ContentBlockResolver
from core.model_tasks import SpeechExecutionError
from core.utils.paths import model_path
from tests.core.chat.block_resolver_test_support import (
    IMAGE_WIRE,
    TEXT_IMAGE,
    TEXT_IMAGE_AUDIO,
    _media_message,
    _resolve,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt wav-payload"


OGG_BYTES = b"OggS\x00\x02voice-payload"


class _StubTranscriber:
    def __init__(self, text: str = "hello from speech") -> None:
        self.text = text
        self.calls: list[tuple[str, str]] = []

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> object:
        self.calls.append((filename, media_type))
        return SimpleNamespace(text=self.text)


class _FailingTranscriber:
    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> object:
        raise SpeechExecutionError("provider unavailable")


def test_current_turn_native_audio_resolves_to_base64_for_audio_model(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("clip.wav", WAV_BYTES)
    transcriber = _StubTranscriber()
    resolver = ContentBlockResolver(store, transcriber=transcriber)
    messages = [_media_message(record)]

    # Act
    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE_AUDIO,
    )

    # Assert
    assert resolved[0]["content"] == [
        {
            "type": "media",
            "base64": base64.b64encode(WAV_BYTES).decode("ascii"),
            "media_type": "audio/wav",
        },
        {
            "type": "text",
            "text": f"[Audio: clip.wav (audio/wav) — Path: {model_path(record.file_path)}]",
        },
    ]
    assert transcriber.calls == []


def test_current_turn_audio_degrades_to_transcription_without_audio_modality(
    tmp_path: Path,
) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", OGG_BYTES)
    transcriber = _StubTranscriber(text="hallo welt")
    resolver = ContentBlockResolver(store, transcriber=transcriber)
    messages = [_media_message(record)]

    # Act
    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE,
    )

    # Assert
    assert resolved[0]["content"] == [
        {
            "type": "text",
            "text": (
                "[Audio attachment voice.ogg (audio/ogg) — automatic transcription, "
                "may contain recognition errors]:\nhallo welt"
            ),
        },
        {
            "type": "text",
            "text": f"[Audio: voice.ogg (audio/ogg) — Path: {model_path(record.file_path)}]",
        },
    ]
    assert transcriber.calls == [("voice.ogg", "audio/ogg")]
    assert store.get(record.id).transcription == "hallo welt"


def test_current_turn_ogg_audio_degrades_even_for_audio_model(tmp_path: Path) -> None:
    # Ogg is outside the OpenAI input_audio format set, so the native path is gated off.
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", OGG_BYTES)
    transcriber = _StubTranscriber(text="ogg transcript")
    resolver = ContentBlockResolver(store, transcriber=transcriber)
    messages = [_media_message(record)]

    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE_AUDIO,
    )

    assert transcriber.calls == [("voice.ogg", "audio/ogg")]
    assert resolved[0]["content"][0]["type"] == "text"
    assert "ogg transcript" in resolved[0]["content"][0]["text"]


def test_audio_model_on_image_only_wire_degrades_to_transcription(tmp_path: Path) -> None:
    # The model advertises audio, but the adapter wire carries images only. The
    # resolver must degrade to STT rather than emit native audio the wire cannot
    # encode — the latent resolver/adapter contradiction, now closed.
    store = AttachmentStore(tmp_path)
    record = store.store("clip.wav", WAV_BYTES)
    transcriber = _StubTranscriber(text="from stt")
    resolver = ContentBlockResolver(store, transcriber=transcriber)
    messages = [_media_message(record)]

    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE_AUDIO,
        wire_media_types=IMAGE_WIRE,
    )

    assert transcriber.calls == [("clip.wav", "audio/wav")]
    assert resolved[0]["content"][0]["type"] == "text"
    assert "from stt" in resolved[0]["content"][0]["text"]


def test_audio_model_on_image_only_wire_without_transcriber_degrades_to_path_note(
    tmp_path: Path,
) -> None:
    # Same contradiction, no transcriber available: the audio cannot be carried
    # and cannot be transcribed, so it degrades to a path note instead of aborting.
    store = AttachmentStore(tmp_path)
    record = store.store("clip.wav", WAV_BYTES)
    resolver = ContentBlockResolver(store)
    messages = [_media_message(record)]

    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE_AUDIO,
        wire_media_types=IMAGE_WIRE,
    )

    assert resolved[0]["content"] == [
        {
            "type": "text",
            "text": (
                "[Audio: clip.wav (audio/wav) — this model cannot accept audio and "
                "no speech-to-text service is available, so only the stored file "
                f"path is provided — Path: {model_path(record.file_path)}]"
            ),
        }
    ]


def test_cached_transcription_is_reused_without_new_stt_call(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", OGG_BYTES)
    store.set_transcription(record.id, "cached words")
    transcriber = _StubTranscriber()
    resolver = ContentBlockResolver(store, transcriber=transcriber)
    messages = [_media_message(record)]

    # Act
    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE,
    )

    # Assert
    assert transcriber.calls == []
    assert "cached words" in resolved[0]["content"][0]["text"]


def test_historical_audio_with_cached_transcription_embeds_transcript(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", OGG_BYTES)
    store.set_transcription(record.id, "what was said")
    resolver = ContentBlockResolver(store, transcriber=_StubTranscriber())
    messages = [_media_message(record, message_id="user-historical")]

    # Act
    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="other-message",
        input_modalities=TEXT_IMAGE,
    )

    # Assert
    assert "what was said" in resolved[0]["content"][0]["text"]


def test_historical_audio_without_transcription_resolves_to_path_note(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("clip.wav", WAV_BYTES)
    resolver = ContentBlockResolver(store, transcriber=_StubTranscriber())
    messages = [_media_message(record, message_id="user-historical")]

    # Act
    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="other-message",
        input_modalities=TEXT_IMAGE_AUDIO,
    )

    # Assert
    assert resolved[0]["content"] == [
        {
            "type": "text",
            "text": (
                "[Audio from an earlier turn: clip.wav (audio/wav) — "
                f"Path: {model_path(record.file_path)}]"
            ),
        }
    ]


def test_current_turn_audio_without_transcriber_degrades_to_path_note(tmp_path: Path) -> None:
    # A non-audio model with no speech-to-text service does not abort the run:
    # the audio degrades to a path note so the agent can still route the file.
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", OGG_BYTES)
    resolver = ContentBlockResolver(store)
    messages = [_media_message(record)]

    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE,
    )

    assert resolved[0]["content"] == [
        {
            "type": "text",
            "text": (
                "[Audio: voice.ogg (audio/ogg) — this model cannot accept audio and "
                "no speech-to-text service is available, so only the stored file "
                f"path is provided — Path: {model_path(record.file_path)}]"
            ),
        }
    ]


def test_current_turn_audio_transcription_failure_degrades_to_path_note(tmp_path: Path) -> None:
    # A speech-to-text failure degrades to a path note instead of aborting the run.
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", OGG_BYTES)
    resolver = ContentBlockResolver(store, transcriber=_FailingTranscriber())
    messages = [_media_message(record)]

    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE,
    )

    assert resolved[0]["content"] == [
        {
            "type": "text",
            "text": (
                "[Audio: voice.ogg (audio/ogg) — speech-to-text could not transcribe "
                "this audio, so only the stored file path is provided — "
                f"Path: {model_path(record.file_path)}]"
            ),
        }
    ]


def test_current_turn_audio_empty_transcription_degrades_to_path_note(tmp_path: Path) -> None:
    # An empty speech-to-text result degrades to a path note rather than aborting.
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", OGG_BYTES)
    resolver = ContentBlockResolver(store, transcriber=_StubTranscriber(text="   "))
    messages = [_media_message(record)]

    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE,
    )

    assert resolved[0]["content"] == [
        {
            "type": "text",
            "text": (
                "[Audio: voice.ogg (audio/ogg) — speech-to-text could not transcribe "
                "this audio, so only the stored file path is provided — "
                f"Path: {model_path(record.file_path)}]"
            ),
        }
    ]
