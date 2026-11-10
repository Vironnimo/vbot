"""Capture high-confidence credentials from human-authored message text."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.chat.content_blocks import TextBlock, masked_content
from core.chat.errors import ChatError
from core.secrets.detection import detect_api_keys
from core.utils.workers import settle_before_cancelling

if TYPE_CHECKING:
    from core.chat._run_state import ChatLoopDependencies
    from core.chat.content_blocks import ContentBlock
    from core.sessions import SessionAddress


async def capture_user_input(
    deps: ChatLoopDependencies, address: SessionAddress, content: str | list[ContentBlock]
) -> str | list[ContentBlock]:
    texts = (
        [content]
        if isinstance(content, str)
        else [block.text for block in content if isinstance(block, TextBlock)]
    )
    candidates = [(match.name, match.value) for text in texts for match in detect_api_keys(text)]
    if candidates:
        if deps.secrets is None:
            raise ChatError("Secret storage is unavailable; the message was not sent.")
        await settle_before_cancelling(
            asyncio.to_thread(deps.secrets.capture_api_keys, address, candidates)
        )
    return masked_content(content, deps.secret_masker)
