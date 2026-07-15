"""Canonical source-derived Passage construction for semantic Recall."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from core.recall.jsonl import (
    SESSION_RECALL_DEFAULT_ROLES,
    is_recall_artifact_message,
    message_search_text,
)
from core.sessions import is_skill_context_note

PASSAGE_POLICY_VERSION = 1
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


@dataclass(frozen=True)
class _Fragment:
    message_id: str
    timestamp: str
    role: str
    text: str
    stream_start: int
    stream_end: int


def build_session_passages(
    messages: Iterable[Any],
    *,
    roles: tuple[str, ...] = SESSION_RECALL_DEFAULT_ROLES,
    target_chars: int = PASSAGE_TARGET_CHARS,
    overlap_chars: int = PASSAGE_OVERLAP_CHARS,
) -> list[Passage]:
    """Split eligible Session text into overlapping Passages without source truncation."""

    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= target_chars:
        raise ValueError("overlap_chars must be non-negative and smaller than target_chars")

    stream_parts: list[str] = []
    fragments: list[_Fragment] = []
    stream_length = 0
    for message in messages:
        if getattr(message, "role", "") not in roles:
            continue
        if is_skill_context_note(message) or is_recall_artifact_message(message):
            continue
        text = message_search_text(message)
        if not text:
            continue
        if stream_parts:
            separator = "\n\n"
            stream_parts.append(separator)
            stream_length += len(separator)
        start = stream_length
        stream_parts.append(text)
        stream_length += len(text)
        fragments.append(
            _Fragment(
                message_id=str(message.id),
                timestamp=str(message.timestamp),
                role=str(message.role),
                text=text,
                stream_start=start,
                stream_end=stream_length,
            )
        )

    if not fragments:
        return []
    stream = "".join(stream_parts)
    passages: list[Passage] = []
    step = target_chars - overlap_chars
    window_start = 0
    while window_start < len(stream):
        window_end = min(window_start + target_chars, len(stream))
        intersecting = [
            fragment
            for fragment in fragments
            if fragment.stream_end > window_start and fragment.stream_start < window_end
        ]
        if intersecting:
            first = intersecting[0]
            last = intersecting[-1]
            start_offset = max(window_start - first.stream_start, 0)
            end_offset = min(window_end - last.stream_start, len(last.text))
            text = stream[window_start:window_end]
            passages.append(
                Passage(
                    passage_id=_passage_id(first, last, start_offset, end_offset),
                    text=text,
                    start_message_id=first.message_id,
                    end_message_id=last.message_id,
                    start_timestamp=first.timestamp,
                    end_timestamp=last.timestamp,
                    start_role=first.role,
                    end_role=last.role,
                    start_offset=start_offset,
                    end_offset=end_offset,
                )
            )
        if window_end >= len(stream):
            break
        window_start += step
    return passages


def _passage_id(first: _Fragment, last: _Fragment, start_offset: int, end_offset: int) -> str:
    identity = (
        f"v{PASSAGE_POLICY_VERSION}\0{first.message_id}\0{start_offset}\0"
        f"{last.message_id}\0{end_offset}"
    ).encode()
    return hashlib.sha256(identity).hexdigest()[:32]


__all__ = [
    "PASSAGE_OVERLAP_CHARS",
    "PASSAGE_POLICY_VERSION",
    "PASSAGE_TARGET_CHARS",
    "Passage",
    "build_session_passages",
]
