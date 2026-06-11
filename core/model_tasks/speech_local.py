"""Local speech execution extension points."""

from __future__ import annotations

from typing import Any

from core.model_tasks.speech_types import SpeechSynthesisResult, SpeechTranscriptionResult
from core.utils.errors import VBotError


class LocalSpeechError(VBotError):
    """Raised for local speech target execution errors."""


class LocalSpeechExecutor:
    """Default local speech executor for future Whisper/Piper integrations."""

    async def transcribe(
        self,
        local_id: str,
        audio: bytes,
        *,
        filename: str,
        media_type: str,
        options: dict[str, Any],
    ) -> SpeechTranscriptionResult:
        raise LocalSpeechError(f"Local speech-to-text target is not available: {local_id}")

    async def synthesize(
        self,
        local_id: str,
        text: str,
        *,
        options: dict[str, Any],
    ) -> SpeechSynthesisResult:
        raise LocalSpeechError(f"Local text-to-speech target is not available: {local_id}")
