"""Openrouter constants."""

from __future__ import annotations

import re

from core.utils.logging import get_logger

OPENROUTER_RESPONSES_ENDPOINT = "/responses"

_OPENROUTER_SHARED_POLICY_STATUSES = frozenset({401, 403, 429, 502, 503, 504})

MAX_REASONING_PARAGRAPH_NEWLINES = 2

REASONING_NEWLINE_RUN_PATTERN = re.compile(r"\n{3,}")

_REASONING_TRAILING_NEWLINES_STATE_KEY = "openrouter_reasoning_trailing_newlines"

OPENROUTER_CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}

OPENROUTER_CACHE_BREAKPOINT_LIMIT = 4

OPENROUTER_MAX_HISTORY_CACHE_BREAKPOINTS = 3

SUPPLEMENTARY_OUTPUT_MODALITIES = (
    "transcription",
    "speech",
    "image",
    "audio",
    "video",
    "embeddings",
    "decisions",
)

IMAGE_MODELS_ENDPOINT = "/images/models"

VIDEO_MODELS_ENDPOINT = "/videos/models"

_IMAGE_DETAIL_CONCURRENCY = 8

_LOGGER = get_logger("providers.openrouter")
