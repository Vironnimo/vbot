"""Tests for chat-layer attachment content block resolution."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from core.attachments import AttachmentStore
from core.chat import ChatError, ChatLoop, ChatMessage, ChatSession
from core.chat.block_resolver import ContentBlockResolver
from core.chat.content_blocks import MediaBlock
from core.model_tasks import SpeechExecutionError

TEXT_IMAGE = frozenset({"text", "image"})
TEXT_ONLY = frozenset({"text"})
TEXT_IMAGE_AUDIO = frozenset({"text", "image", "audio"})

# Wire-media sets an adapter declares. The image+audio set mirrors a typical
# OpenAI-compatible wire and is the resolve-helper default so existing native-path
# tests read unchanged; the wire gate itself is exercised explicitly below by
# passing a narrower set (e.g. image-only).
IMAGE_WIRE = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
IMAGE_AUDIO_WIRE = IMAGE_WIRE | frozenset({"audio/wav", "audio/mpeg"})
IMAGE_PDF_WIRE = IMAGE_WIRE | frozenset({"application/pdf"})

TEXT_IMAGE_PDF = frozenset({"text", "image", "pdf"})

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n"

WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt wav-payload"
OGG_BYTES = b"OggS\x00\x02voice-payload"
MP4_BYTES = b"\x00\x00\x00\x18ftypisomvideo-payload"


class _StubPrompts:
    def build_system_prompt(
        self,
        _agent: object,
        scope: object = None,
        *,
        agent_body: str = "",
        project_context: object = None,
    ) -> str:
        return "System prompt"

    def render_project_files(self, project_context: object) -> str:
        return "" if project_context is None else "RENDERED-PROJECT-FILES"


class _StubModels:
    """Mirror ``ModelRegistry.get`` enough for input-modality resolution."""

    def get(self, _provider_id: str, _model_id: str) -> object:
        return SimpleNamespace(capabilities=SimpleNamespace(input_modalities=("text", "image")))


class _StubRuntime:
    def __init__(self) -> None:
        self.system_prompts = _StubPrompts()
        self.models = _StubModels()


class _StubAgent:
    def __init__(self, model: str = "openai/gpt-5.2") -> None:
        self.model = model


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


def _media_message(record: Any, *, message_id: str = "user-current") -> dict:
    return {
        "id": message_id,
        "role": "user",
        "content": [
            {
                "type": "media",
                "attachment_id": record.id,
                "filename": record.filename,
                "media_type": record.media_type,
            }
        ],
    }


def _resolve(
    resolver: ContentBlockResolver,
    messages: list[dict],
    *,
    wire_media_types: frozenset[str] = IMAGE_AUDIO_WIRE,
    **kwargs,
) -> list[dict]:
    return asyncio.run(
        resolver.resolve_messages(messages, wire_media_types=wire_media_types, **kwargs)
    )


def test_current_turn_image_media_block_resolves_to_base64(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    image_bytes = b"\x89PNG\r\n\x1a\nimage-bytes"
    record = store.store("photo.png", image_bytes)
    resolver = ContentBlockResolver(store)
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
            "type": "media",
            "base64": base64.b64encode(image_bytes).decode("ascii"),
            "media_type": "image/png",
        }
    ]
    assert messages[0]["content"][0] == {
        "type": "media",
        "attachment_id": record.id,
        "filename": record.filename,
        "media_type": "image/png",
    }


def test_historical_turn_image_resolves_to_placeholder_text(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("old-photo.png", b"\x89PNG\r\n\x1a\nold")
    resolver = ContentBlockResolver(store)
    messages = [_media_message(record, message_id="user-historical")]

    # Act
    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="other-message",
        input_modalities=TEXT_IMAGE,
    )

    # Assert
    assert resolved[0]["content"] == [
        {
            "type": "text",
            "text": (
                f"[Image from an earlier turn: old-photo.png (image/png) "
                f"— Path: {record.file_path}]"
            ),
        }
    ]


def test_historical_turn_image_with_deleted_attachment_degrades_gracefully(
    tmp_path: Path,
) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("gone.png", b"\x89PNG\r\n\x1a\ngone")
    store.delete(record.id)
    resolver = ContentBlockResolver(store)
    messages = [_media_message(record, message_id="user-historical")]

    # Act
    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="other-message",
        input_modalities=TEXT_IMAGE,
    )

    # Assert
    assert resolved[0]["content"] == [
        {
            "type": "text",
            "text": "[Image from an earlier turn: gone.png (image/png) — file no longer available]",
        }
    ]


@pytest.mark.parametrize("current_turn", [True, False])
def test_file_block_resolves_to_text_path_note(tmp_path: Path, current_turn: bool) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("report.pdf", b"%PDF-1.7\n1 0 obj\n")
    resolver = ContentBlockResolver(store)
    message_id = "user-current" if current_turn else "user-historical"
    messages = [
        {
            "id": message_id,
            "role": "user",
            "content": [
                {
                    "type": "file",
                    "attachment_id": record.id,
                    "filename": record.filename,
                    "media_type": record.media_type,
                }
            ],
        }
    ]

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
            "text": (f"[File: report.pdf (application/pdf) — Path: {record.file_path}]"),
        }
    ]


def _file_message(record: Any, *, message_id: str = "user-current") -> dict:
    return {
        "id": message_id,
        "role": "user",
        "content": [
            {
                "type": "file",
                "attachment_id": record.id,
                "filename": record.filename,
                "media_type": record.media_type,
            }
        ],
    }


def test_current_turn_pdf_resolves_to_native_document_block(tmp_path: Path) -> None:
    # A PDF-capable model whose adapter wire carries application/pdf gets the
    # canonical native document block end-to-end.
    store = AttachmentStore(tmp_path)
    record = store.store("report.pdf", PDF_BYTES)
    resolver = ContentBlockResolver(store)
    messages = [_file_message(record)]

    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE_PDF,
        wire_media_types=IMAGE_PDF_WIRE,
    )

    assert resolved[0]["content"] == [
        {
            "type": "document",
            "base64": base64.b64encode(PDF_BYTES).decode("ascii"),
            "media_type": "application/pdf",
            "filename": "report.pdf",
        }
    ]


def test_current_turn_pdf_degrades_to_path_note_without_pdf_modality(tmp_path: Path) -> None:
    # The wire carries PDF, but the model does not advertise the pdf modality.
    store = AttachmentStore(tmp_path)
    record = store.store("report.pdf", PDF_BYTES)
    resolver = ContentBlockResolver(store)
    messages = [_file_message(record)]

    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE,
        wire_media_types=IMAGE_PDF_WIRE,
    )

    assert resolved[0]["content"] == [
        {"type": "text", "text": f"[File: report.pdf (application/pdf) — Path: {record.file_path}]"},
    ]


def test_current_turn_pdf_degrades_to_path_note_when_wire_cannot_carry(tmp_path: Path) -> None:
    # The model advertises pdf, but the chosen adapter wire cannot carry it
    # (e.g. an unverified OpenAI-compatible provider) — degrade, never crash.
    store = AttachmentStore(tmp_path)
    record = store.store("report.pdf", PDF_BYTES)
    resolver = ContentBlockResolver(store)
    messages = [_file_message(record)]

    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE_PDF,
        wire_media_types=IMAGE_WIRE,
    )

    assert resolved[0]["content"][0]["type"] == "text"
    assert "report.pdf" in resolved[0]["content"][0]["text"]


def test_historical_pdf_degrades_to_path_note_even_when_native_capable(tmp_path: Path) -> None:
    # An earlier-turn PDF is never re-sent natively, regardless of capability.
    store = AttachmentStore(tmp_path)
    record = store.store("report.pdf", PDF_BYTES)
    resolver = ContentBlockResolver(store)
    messages = [_file_message(record, message_id="user-historical")]

    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="other-message",
        input_modalities=TEXT_IMAGE_PDF,
        wire_media_types=IMAGE_PDF_WIRE,
    )

    assert resolved[0]["content"][0]["type"] == "text"
    assert "report.pdf" in resolved[0]["content"][0]["text"]


@pytest.mark.parametrize("current_turn", [True, False])
def test_text_block_resolves_to_text_dict(tmp_path: Path, current_turn: bool) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    resolver = ContentBlockResolver(store)
    message_id = "user-current" if current_turn else "user-historical"
    messages = [
        {
            "id": message_id,
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
        }
    ]

    # Act
    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE,
    )

    # Assert
    assert resolved[0]["content"] == [{"type": "text", "text": "hello"}]


def test_current_turn_image_raises_when_vision_not_supported(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("photo.png", b"\x89PNG\r\n\x1a\nimage")
    resolver = ContentBlockResolver(store)
    messages = [_media_message(record)]

    # Act / Assert
    with pytest.raises(
        ChatError,
        match="Model does not support vision; cannot process image attachment",
    ):
        _resolve(
            resolver,
            messages,
            current_user_message_id="user-current",
            input_modalities=TEXT_ONLY,
        )


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
        }
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
        }
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


def test_audio_model_on_image_only_wire_without_transcriber_raises(tmp_path: Path) -> None:
    # Same contradiction, no transcriber available: a clear ChatError instead of
    # the adapter being handed audio it cannot carry.
    store = AttachmentStore(tmp_path)
    record = store.store("clip.wav", WAV_BYTES)
    resolver = ContentBlockResolver(store)
    messages = [_media_message(record)]

    with pytest.raises(ChatError, match="no speech-to-text"):
        _resolve(
            resolver,
            messages,
            current_user_message_id="user-current",
            input_modalities=TEXT_IMAGE_AUDIO,
            wire_media_types=IMAGE_WIRE,
        )


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
                f"[Audio from an earlier turn: clip.wav (audio/wav) — Path: {record.file_path}]"
            ),
        }
    ]


def test_current_turn_audio_without_transcriber_raises_clear_error(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", OGG_BYTES)
    resolver = ContentBlockResolver(store)
    messages = [_media_message(record)]

    # Act / Assert
    with pytest.raises(ChatError, match="no speech-to-text"):
        _resolve(
            resolver,
            messages,
            current_user_message_id="user-current",
            input_modalities=TEXT_IMAGE,
        )


def test_current_turn_audio_transcription_failure_raises_chat_error(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("voice.ogg", OGG_BYTES)
    resolver = ContentBlockResolver(store, transcriber=_FailingTranscriber())
    messages = [_media_message(record)]

    # Act / Assert
    with pytest.raises(ChatError, match="could not be transcribed"):
        _resolve(
            resolver,
            messages,
            current_user_message_id="user-current",
            input_modalities=TEXT_IMAGE,
        )


@pytest.mark.parametrize("current_turn", [True, False])
def test_video_block_resolves_to_path_note(tmp_path: Path, current_turn: bool) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("clip.mp4", MP4_BYTES)
    resolver = ContentBlockResolver(store)
    message_id = "user-current" if current_turn else "user-historical"
    messages = [_media_message(record, message_id=message_id)]

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
            "type": "text",
            "text": f"[Video: clip.mp4 (video/mp4) — Path: {record.file_path}]",
        }
    ]


def test_mixed_text_and_image_blocks_resolve_in_order(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    image_bytes = b"\x89PNG\r\n\x1a\nmixed"
    record = store.store("photo.png", image_bytes)
    resolver = ContentBlockResolver(store)
    messages = [
        {
            "id": "user-current",
            "role": "user",
            "content": [
                {"type": "text", "text": "Analyze this image:"},
                {
                    "type": "media",
                    "attachment_id": record.id,
                    "filename": record.filename,
                    "media_type": record.media_type,
                },
            ],
        }
    ]

    # Act
    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="user-current",
        input_modalities=TEXT_IMAGE,
    )

    # Assert
    assert resolved[0]["content"] == [
        {"type": "text", "text": "Analyze this image:"},
        {
            "type": "media",
            "base64": base64.b64encode(image_bytes).decode("ascii"),
            "media_type": "image/png",
        },
    ]


def test_string_content_messages_pass_through_unmodified(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    resolver = ContentBlockResolver(store)
    messages = [
        {"id": "sys", "role": "system", "content": "System prompt"},
        {"id": "u1", "role": "user", "content": "Simple text"},
    ]

    # Act
    resolved = _resolve(
        resolver,
        messages,
        current_user_message_id="u1",
        input_modalities=TEXT_IMAGE,
    )

    # Assert
    assert resolved == messages


def test_chat_loop_resolves_historical_blocks_when_latest_user_turn_is_plain_text(
    tmp_path: Path,
) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("old-photo.png", b"\x89PNG\r\n\x1a\nold")
    session = ChatSession.create(tmp_path / "sessions", session_id="session-one")
    session.append(
        ChatMessage.user(
            [
                MediaBlock(
                    type="media",
                    attachment_id=record.id,
                    filename=record.filename,
                    media_type=record.media_type,
                )
            ]
        )
    )
    session.append(ChatMessage.user("latest plain text"))
    runtime: Any = _StubRuntime()
    loop = ChatLoop(runtime, attachment_resolver=ContentBlockResolver(store))

    # Act
    request_messages = asyncio.run(loop._build_request_messages(_StubAgent(), session))

    # Assert
    assert [message["role"] for message in request_messages] == ["system", "user", "user"]
    assert request_messages[1]["content"] == [
        {
            "type": "text",
            "text": (
                f"[Image from an earlier turn: old-photo.png (image/png) "
                f"— Path: {record.file_path}]"
            ),
        }
    ]
    assert request_messages[2]["content"] == "latest plain text"


def test_chat_loop_skips_resolver_when_session_has_only_plain_text_user_messages(
    tmp_path: Path,
) -> None:
    # Arrange
    session = ChatSession.create(tmp_path / "sessions", session_id="session-one")
    session.append(ChatMessage.user("first"))
    session.append(ChatMessage.user("second"))
    resolver = Mock()
    runtime: Any = _StubRuntime()
    loop = ChatLoop(runtime, attachment_resolver=resolver)

    # Act
    request_messages = asyncio.run(loop._build_request_messages(_StubAgent(), session))

    # Assert
    resolver.resolve_messages.assert_not_called()
    assert [message["role"] for message in request_messages] == ["system", "user", "user"]
    assert request_messages[1]["content"] == "first"
    assert request_messages[2]["content"] == "second"
