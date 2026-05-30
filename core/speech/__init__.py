"""Speech-to-text and text-to-speech execution services."""

from core.speech.local import LocalSpeechError, LocalSpeechExecutor
from core.speech.providers import ProviderSpeechClient, audio_format_from
from core.speech.speech import (
    SpeechArtifact,
    SpeechConfigurationError,
    SpeechError,
    SpeechExecutionError,
    SpeechService,
    SpeechUnsupportedTargetError,
)
from core.speech.types import SpeechSynthesisResult, SpeechTranscriptionResult

__all__ = [
    "LocalSpeechError",
    "LocalSpeechExecutor",
    "ProviderSpeechClient",
    "SpeechArtifact",
    "SpeechConfigurationError",
    "SpeechError",
    "SpeechExecutionError",
    "SpeechService",
    "SpeechSynthesisResult",
    "SpeechTranscriptionResult",
    "SpeechUnsupportedTargetError",
    "audio_format_from",
]
