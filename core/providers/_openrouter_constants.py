"""Openrouter constants."""

from __future__ import annotations

import re

from core.utils.logging import get_logger

OPENROUTER_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}

OPENROUTER_REASONING_OFF = {"enabled": False}

OPENROUTER_NONE_EFFORT = "none"

OPENROUTER_RESPONSES_ENDPOINT = "/responses"

OPENROUTER_ALL_TURNS_RESPONSES_MODELS = frozenset(
    {
        "openai/gpt-5.6-luna",
        "openai/gpt-5.6-luna-pro",
        "openai/gpt-5.6-sol",
        "openai/gpt-5.6-sol-pro",
        "openai/gpt-5.6-terra",
        "openai/gpt-5.6-terra-pro",
    }
)

OPENROUTER_RESPONSES_REQUEST_PARAMETERS = frozenset({"max_tokens", "max_output_tokens", "top_p"})

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
)

IMAGE_MODELS_ENDPOINT = "/images/models"

VIDEO_MODELS_ENDPOINT = "/videos/models"

_IMAGE_DETAIL_CONCURRENCY = 8

_LOGGER = get_logger("providers.openrouter")
