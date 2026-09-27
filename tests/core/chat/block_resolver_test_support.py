"""Shared messages and a synchronous resolve helper for block resolver tests."""

from __future__ import annotations

import asyncio
from typing import Any

from core.chat.block_resolver import ContentBlockResolver
from core.utils.paths import model_path

TEXT_ONLY = frozenset({"text"})
TEXT_IMAGE = frozenset({"text", "image"})
TEXT_IMAGE_AUDIO = frozenset({"text", "image", "audio"})

# Wire-media sets an adapter declares. The image+audio set mirrors a typical
# OpenAI-compatible wire and is the resolve helper's default.
IMAGE_WIRE = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
IMAGE_AUDIO_WIRE = IMAGE_WIRE | frozenset({"audio/wav", "audio/mpeg"})

# The resolver treats the message with this id as the current turn; every other
# user message is an earlier turn.
CURRENT = "user-current"
EARLIER = "user-earlier"


def attachment_message(
    record: Any,
    *,
    block_type: str = "media",
    message_id: str = CURRENT,
    **fields: Any,
) -> dict:
    return {
        "id": message_id,
        "role": "user",
        "content": [
            {
                "type": block_type,
                "attachment_id": record.id,
                "filename": record.filename,
                "media_type": record.media_type,
                **fields,
            }
        ],
    }


def path_note(record: Any, template: str) -> dict:
    """Render a path-note text block; ``template`` may use ``{path}``."""
    return {"type": "text", "text": template.format(path=model_path(record.file_path))}


def resolve(
    resolver: ContentBlockResolver,
    messages: list[dict],
    *,
    input_modalities: frozenset[str],
    wire_media_types: frozenset[str] = IMAGE_AUDIO_WIRE,
) -> list[dict]:
    return asyncio.run(
        resolver.resolve_messages(
            messages,
            current_user_message_id=CURRENT,
            input_modalities=input_modalities,
            wire_media_types=wire_media_types,
        )
    )
