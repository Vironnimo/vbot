"""Capture high-confidence credentials from human-authored message text."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from core.chat.content_blocks import TextBlock, masked_content
from core.chat.errors import ChatError
from core.chat.messages import ChatMessage
from core.runs import RunKind
from core.secrets.agent_secrets import SecretMasker
from core.secrets.detection import detect_api_keys
from core.utils.workers import settle_before_cancelling

if TYPE_CHECKING:
    from core.chat._run_state import ChatLoopDependencies
    from core.chat.content_blocks import ContentBlock
    from core.chat.messages import MessageSender, ReplySurface
    from core.sessions import SessionAddress


async def prepare_run_input(
    deps: ChatLoopDependencies,
    address: SessionAddress,
    content: str | list[ContentBlock] | None,
    *,
    internal: bool,
    sender: MessageSender | None,
    reply_surface: ReplySurface | None,
    run_kind: RunKind,
) -> tuple[str | list[ContentBlock] | None, Callable[[], SecretMasker]]:
    """Capture only direct human input. Mask all inputs for their receiving Session."""
    passed_only = await deps.input_passed_only(address)
    current_masker = partial(deps.secret_masker_for, address, passed_only=passed_only)
    if content is not None:
        direct = reply_surface is not None and (
            (run_kind == RunKind.USER and reply_surface.kind in {"webui", "tui"})
            or (
                run_kind in {RunKind.USER, RunKind.CHANNEL}
                and reply_surface.kind == "channel"
                and reply_surface.conversation_kind == "direct"
            )
        )
        if direct and not internal and (sender is None or sender.kind == "user"):
            content = await capture_user_input(deps, address, content, passed_only=passed_only)
        else:
            content = masked_content(content, current_masker())
    return content, current_masker


async def capture_user_input(
    deps: ChatLoopDependencies,
    address: SessionAddress,
    content: str | list[ContentBlock],
    *,
    passed_only: bool | None = None,
) -> str | list[ContentBlock]:
    if passed_only is None:
        passed_only = await deps.input_passed_only(address)
    texts = (
        [content]
        if isinstance(content, str)
        else [block.text for block in content if isinstance(block, TextBlock)]
    )
    candidates = [(match.name, match.value) for text in texts for match in detect_api_keys(text)]
    if candidates:
        if deps.secrets is None:
            raise ChatError("Secret storage is unavailable; the message was not sent.")
        references = await settle_before_cancelling(
            asyncio.to_thread(
                deps.secrets.capture_api_keys, address, candidates, passed_only=passed_only
            )
        )
        # Another Session can hold its own copy of a Tool-discovered key under
        # a different name. Prefer references this receiving Session can use.
        content = masked_content(content, SecretMasker(references))
    return masked_content(content, deps.secret_masker_for(address, passed_only=passed_only))


def masked_context_note(
    note: str | ChatMessage | None, masker: SecretMasker
) -> str | ChatMessage | None:
    if isinstance(note, str):
        return masker.mask(note)
    if note is not None and isinstance(note.content, str):
        return replace(note, content=masker.mask(note.content))
    return note
