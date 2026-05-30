"""Shared task-model vocabulary."""

from __future__ import annotations

TASK_SPEECH_TO_TEXT = "speech_to_text"
TASK_TEXT_TO_SPEECH = "text_to_speech"
TASK_IMAGE_GENERATION = "image_generation"
TASK_IMAGE_EDIT = "image_edit"
TASK_VIDEO_GENERATION = "video_generation"

SUPPORTED_TASK_TYPES = frozenset(
    {
        TASK_SPEECH_TO_TEXT,
        TASK_TEXT_TO_SPEECH,
        TASK_IMAGE_GENERATION,
        TASK_VIDEO_GENERATION,
    }
)

SPEECH_TASK_TYPES = frozenset({TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH})
