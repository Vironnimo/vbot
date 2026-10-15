"""Canonical source-derived Passage construction for semantic Recall."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from core.sessions.search_content import (
    SESSION_RECALL_DEFAULT_ROLES,
    message_is_searchable,
    message_search_text,
)

PASSAGE_POLICY_VERSION = 2
PASSAGE_TARGET_CHARS = 1500
PASSAGE_OVERLAP_CHARS = 200


@dataclass(frozen=True)
class Passage:
    """One searchable span with exact canonical Message boundaries."""

    passage_id: str
    text: str
    start_message_id: str
    end_message_id: str
    start_timestamp: str
    end_timestamp: str
    start_role: str
    end_role: str
    start_offset: int
    end_offset: int


def build_session_passages(
    messages: Iterable[Any],
    *,
    roles: tuple[str, ...] = SESSION_RECALL_DEFAULT_ROLES,
    target_chars: int = PASSAGE_TARGET_CHARS,
    overlap_chars: int = PASSAGE_OVERLAP_CHARS,
) -> list[Passage]:
    """Build entry-anchored windows; adjacent entries cannot change their identity.

    Forks share canonical entries, not a concatenated conversation string. A
    window crossing entries would change when either branch continues and make
    shared history searchable twice. Each entry therefore owns its windows;
    the result's canonical conversation context supplies adjacent turns.
    """
    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= target_chars:
        raise ValueError("overlap_chars must be non-negative and smaller than target_chars")

    passages: list[Passage] = []
    step = target_chars - overlap_chars
    for message in messages:
        if getattr(message, "role", "") not in roles or not message_is_searchable(message):
            continue
        text = message_search_text(message)
        for start in range(0, len(text), step):
            end = min(start + target_chars, len(text))
            identity = f"v{PASSAGE_POLICY_VERSION}\0{message.id}\0{start}\0{end}".encode()
            passages.append(
                Passage(
                    passage_id=hashlib.sha256(identity).hexdigest()[:32],
                    text=text[start:end],
                    start_message_id=str(message.id),
                    end_message_id=str(message.id),
                    start_timestamp=str(message.timestamp),
                    end_timestamp=str(message.timestamp),
                    start_role=str(message.role),
                    end_role=str(message.role),
                    start_offset=start,
                    end_offset=end,
                )
            )
            if end == len(text):
                break
    return passages


__all__ = [
    "PASSAGE_OVERLAP_CHARS",
    "PASSAGE_POLICY_VERSION",
    "PASSAGE_TARGET_CHARS",
    "Passage",
    "build_session_passages",
]
