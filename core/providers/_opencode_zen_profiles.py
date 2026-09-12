"""Opencode zen profiles."""

from __future__ import annotations

OPENCODE_ZEN_METADATA_KEY = "opencode_zen"

PROTOCOL_METADATA_KEY = "protocol"

PRIVACY_METADATA_KEY = "privacy"

DEPRECATES_AT_METADATA_KEY = "deprecates_at"

PROTOCOL_RESPONSES = "responses"

PROTOCOL_MESSAGES = "messages"

PROTOCOL_CHAT = "chat_completions"

PROTOCOL_GEMINI = "gemini_generate_content"

_KNOWN_PROTOCOLS = frozenset(
    {PROTOCOL_RESPONSES, PROTOCOL_MESSAGES, PROTOCOL_CHAT, PROTOCOL_GEMINI}
)

_RESPONSES_MODELS = frozenset(
    {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.4",
        "gpt-5.4-pro",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "gpt-5.2",
        "gpt-5.2-codex",
        "gpt-5.1",
        "gpt-5.1-codex",
        "gpt-5.1-codex-max",
        "gpt-5.1-codex-mini",
        "gpt-5",
        "gpt-5-codex",
        "gpt-5-nano",
        "grok-4.5",
        "grok-build-0.1",
    }
)

_MESSAGES_MODELS = frozenset(
    {
        "claude-fable-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-opus-4-1",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-sonnet-4",
        "claude-haiku-4-5",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.6-plus",
        "qwen3.5-plus",
    }
)

_GEMINI_MODELS = frozenset(
    {
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-pro",
        "gemini-3-flash",
    }
)

_CHAT_MODELS = frozenset(
    {
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "minimax-m3",
        "minimax-m2.7",
        "minimax-m2.5",
        "glm-5.2",
        "glm-5.1",
        "glm-5",
        "kimi-k3",
        "kimi-k2.7-code",
        "kimi-k2.6",
        "kimi-k2.5",
        "big-pickle",
        "mimo-v2.5-free",
        "laguna-s-2.1-free",
        "ling-3.0-flash-free",
        "north-mini-code-free",
        "nemotron-3-ultra-free",
        "deepseek-v4-flash-free",
    }
)

_PROTOCOL_BY_MODEL = {
    **dict.fromkeys(_RESPONSES_MODELS, PROTOCOL_RESPONSES),
    **dict.fromkeys(_MESSAGES_MODELS, PROTOCOL_MESSAGES),
    **dict.fromkeys(_GEMINI_MODELS, PROTOCOL_GEMINI),
    **dict.fromkeys(_CHAT_MODELS, PROTOCOL_CHAT),
}

_FREE_MODELS = frozenset(
    model_id for model_id in _CHAT_MODELS if model_id == "big-pickle" or model_id.endswith("-free")
)

_IMMINENT_DEPRECATIONS = {
    "claude-opus-4-1": "2026-08-05",
    "kimi-k2.5": "2026-08-05",
    "minimax-m2.5": "2026-08-05",
}

_RETIRED_MODELS = frozenset(
    {
        "gpt-5.2-codex",
        "gpt-5.1-codex",
        "gpt-5.1-codex-max",
        "gpt-5.1-codex-mini",
        "gpt-5-codex",
        "claude-sonnet-4",
        "glm-5",
    }
)

_ZEN_GEMINI_MEDIA_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/heic",
        "image/heif",
        "audio/wav",
        "audio/mp3",
        "audio/mpeg",
        "audio/aiff",
        "audio/aac",
        "audio/ogg",
        "audio/flac",
        "video/mp4",
        "video/mpeg",
        "video/quicktime",
        "video/avi",
        "video/x-flv",
        "video/mpg",
        "video/webm",
        "video/wmv",
        "video/3gpp",
        "application/pdf",
    }
)

_ZEN_INLINE_REQUEST_MAX_BYTES = 20_000_000

_ZEN_MAX_IMAGES_PER_REQUEST = 3_600

_PERMANENT_429_MARKERS = (
    "freeusagelimiterror",
    "gousagelimiterror",
    "blackusagelimiterror",
    "monthly limit",
    "weekly limit",
    "usage limit",
    "quota exceeded",
)

_NON_AUTH_401_MARKERS = (
    "creditserror",
    "monthlylimiterror",
    "userlimiterror",
    "modelerror",
)

_AUTH_401_MARKERS = ("autherror", "invalid api key", "missing api key")
