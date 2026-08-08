"""Shared image result dataclasses without service dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ImageInput:
    """One local image loaded for a provider image-to-image request."""

    filename: str
    media_type: str
    data: bytes


@dataclass(frozen=True)
class ImageGenerationResult:
    """Normalized image generation result."""

    images: tuple[bytes, ...]
    media_type: str
    model: str
    usage: JsonObject | None = None
    raw: JsonObject | None = None

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "model": self.model,
            "media_type": self.media_type,
            "image_count": len(self.images),
        }
        if self.usage is not None:
            payload["usage"] = dict(self.usage)
        return payload


@dataclass(frozen=True)
class ImageUnderstandingResult:
    """Normalized text analysis of one or more local images."""

    content: str
    model: str
    image_count: int
    usage: JsonObject | None = None

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "analysis": self.content,
            "model": self.model,
            "image_count": self.image_count,
        }
        if self.usage is not None:
            payload["usage"] = dict(self.usage)
        return payload


@dataclass(frozen=True)
class ImageUnderstandingRunContext:
    """Run identity needed to correlate an isolated understanding request."""

    run_id: str
    agent_id: str
    session_id: str
    iteration_number: int


@dataclass(frozen=True)
class ImageArtifact:
    """Generated image persisted in a caller-owned working directory."""

    id: str
    filename: str
    media_type: str
    size_bytes: int
    file_path: Path
    index: int = 0
