"""Shared video generation result dataclasses without service dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class VideoGenerationResult:
    """Normalized generated video content returned by a provider."""

    data: bytes
    media_type: str
    model: str
    job_id: str
    usage: JsonObject | None = None
    raw: JsonObject | None = None
