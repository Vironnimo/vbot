"""Shared Music generation result dataclasses without service dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class MusicGenerationResult:
    """Normalized generated Music audio returned by a provider."""

    data: bytes
    media_type: str
    model: str
    transcript: str = ""
    raw: JsonObject | None = None
