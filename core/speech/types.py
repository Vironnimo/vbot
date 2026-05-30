"""Shared speech result dataclasses without service dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class SpeechTranscriptionResult:
    """Normalized speech-to-text result."""

    text: str
    language: str | None = None
    segments: tuple[JsonObject, ...] = ()
    usage: JsonObject | None = None
    raw: JsonObject | None = None

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {"text": self.text}
        if self.language:
            payload["language"] = self.language
        if self.segments:
            payload["segments"] = [dict(segment) for segment in self.segments]
        if self.usage is not None:
            payload["usage"] = dict(self.usage)
        return payload


@dataclass(frozen=True)
class SpeechSynthesisResult:
    """Normalized text-to-speech result."""

    audio: bytes
    media_type: str
    format: str
    generation_id: str | None = None
