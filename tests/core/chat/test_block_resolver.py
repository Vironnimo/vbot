"""Tests for chat-layer attachment content block resolution."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.attachments import AttachmentStore
from core.chat import ChatError, ChatLoop, ChatMessage, ChatSession
from core.chat.block_resolver import ContentBlockResolver
from core.chat.content_blocks import MediaBlock


class _StubPrompts:
    def build_system_prompt(self, _agent: object) -> str:
        return "System prompt"


class _StubRuntime:
    def __init__(self) -> None:
        self.system_prompts = _StubPrompts()


class _StubAgent:
    def __init__(self, model: str = "openai/gpt-5.2") -> None:
        self.model = model


def test_current_turn_image_media_block_resolves_to_base64(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    image_bytes = b"\x89PNG\r\n\x1a\nimage-bytes"
    record = store.store("photo.png", image_bytes)
    resolver = ContentBlockResolver(store)
    messages = [
        {
            "id": "user-current",
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
    ]

    # Act
    resolved = resolver.resolve_messages(
        messages,
        current_user_message_id="user-current",
        vision_supported=True,
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
    messages = [
        {
            "id": "user-historical",
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
    ]

    # Act
    resolved = resolver.resolve_messages(
        messages,
        current_user_message_id="other-message",
        vision_supported=True,
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
    messages = [
        {
            "id": "user-historical",
            "role": "user",
            "content": [
                {
                    "type": "media",
                    "attachment_id": record.id,
                    "filename": "gone.png",
                    "media_type": "image/png",
                }
            ],
        }
    ]

    # Act
    resolved = resolver.resolve_messages(
        messages,
        current_user_message_id="other-message",
        vision_supported=True,
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
    current_id = "user-current"
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
    resolved = resolver.resolve_messages(
        messages,
        current_user_message_id=current_id,
        vision_supported=True,
    )

    # Assert
    assert resolved[0]["content"] == [
        {
            "type": "text",
            "text": (f"[File: report.pdf (application/pdf) — Path: {record.file_path}]"),
        }
    ]


@pytest.mark.parametrize("current_turn", [True, False])
def test_text_block_resolves_to_text_dict(tmp_path: Path, current_turn: bool) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    resolver = ContentBlockResolver(store)
    message_id = "user-current" if current_turn else "user-historical"
    current_id = "user-current"
    messages = [
        {
            "id": message_id,
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
        }
    ]

    # Act
    resolved = resolver.resolve_messages(
        messages,
        current_user_message_id=current_id,
        vision_supported=True,
    )

    # Assert
    assert resolved[0]["content"] == [{"type": "text", "text": "hello"}]


def test_current_turn_image_raises_when_vision_not_supported(tmp_path: Path) -> None:
    # Arrange
    store = AttachmentStore(tmp_path)
    record = store.store("photo.png", b"\x89PNG\r\n\x1a\nimage")
    resolver = ContentBlockResolver(store)
    messages = [
        {
            "id": "user-current",
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
    ]

    # Act / Assert
    with pytest.raises(
        ChatError,
        match="Model does not support vision; cannot process image attachment",
    ):
        resolver.resolve_messages(
            messages,
            current_user_message_id="user-current",
            vision_supported=False,
        )


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
    resolved = resolver.resolve_messages(
        messages,
        current_user_message_id="user-current",
        vision_supported=True,
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
    resolved = resolver.resolve_messages(
        messages,
        current_user_message_id="u1",
        vision_supported=True,
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
    loop = ChatLoop(_StubRuntime(), attachment_resolver=ContentBlockResolver(store))

    # Act
    request_messages = loop._build_request_messages(_StubAgent(), session)

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
    loop = ChatLoop(_StubRuntime(), attachment_resolver=resolver)

    # Act
    request_messages = loop._build_request_messages(_StubAgent(), session)

    # Assert
    resolver.resolve_messages.assert_not_called()
    assert [message["role"] for message in request_messages] == ["system", "user", "user"]
    assert request_messages[1]["content"] == "first"
    assert request_messages[2]["content"] == "second"
