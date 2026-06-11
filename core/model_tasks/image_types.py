"""Shared image result dataclasses without service dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


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
class ImageArtifact:
    """Persisted image generation artifact metadata."""

    id: str
    filename: str
    media_type: str
    size_bytes: int
    file_path: Path
    index: int = 0

    @property
    def url(self) -> str:
        return f"/api/images/artifacts/{self.id}"

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "kind": "image",
            "filename": self.filename,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "url": self.url,
            "index": self.index,
        }
