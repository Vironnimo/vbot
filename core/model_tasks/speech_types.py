"""Shared speech result dataclasses without service dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any

JsonObject = dict[str, Any]


class SpeechProgress:
    """One request's bounded progress snapshot, shared with its inference worker."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._phase = "preparing"
        self._started = monotonic()

    def update(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def snapshot(self) -> JsonObject:
        with self._lock:
            return {"phase": self._phase, "elapsed_seconds": int(monotonic() - self._started)}


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
