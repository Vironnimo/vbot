"""Shared fixtures and fakes for block resolver behavior tests."""

from __future__ import annotations

import asyncio
from typing import Any

from core.chat.block_resolver import ContentBlockResolver

TEXT_IMAGE = frozenset({"text", "image"})


TEXT_IMAGE_AUDIO = frozenset({"text", "image", "audio"})


# Wire-media sets an adapter declares. The image+audio set mirrors a typical
# OpenAI-compatible wire and is the resolve-helper default so existing native-path
# tests read unchanged; the wire gate itself is exercised explicitly below by
# passing a narrower set (e.g. image-only).
IMAGE_WIRE = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


IMAGE_AUDIO_WIRE = IMAGE_WIRE | frozenset({"audio/wav", "audio/mpeg"})


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
