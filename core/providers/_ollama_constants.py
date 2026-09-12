"""Ollama constants."""

from __future__ import annotations

from core.utils.logging import get_logger

_LOGGER = get_logger("providers.ollama")

CHAT_ENDPOINT = "/api/chat"

SHOW_ENDPOINT = "/api/show"

OLLAMA_LOCAL_MODE = "local"

OLLAMA_CLOUD_MODE = "cloud"

OLLAMA_METADATA_KEY = "ollama"

LOCAL_METADATA_FIELD = "local"

REMOTE_METADATA_FIELD = "remote"

_CAPABILITY_TOOLS = "tools"

_CAPABILITY_VISION = "vision"

_CAPABILITY_THINKING = "thinking"

_CAPABILITY_COMPLETION = "completion"

_CAPABILITY_EMBEDDING = "embedding"

OLLAMA_EFFORT_FLOOR = ("low", "medium", "high")

OLLAMA_GPT_OSS_EFFORTS = ("low", "medium", "high")

OLLAMA_CLOUD_REASONING_EFFORTS = ("none", "low", "medium", "high", "max")

_OLLAMA_CLOUD_OPENAI_PATH = "/v1"

_OLLAMA_CLOUD_REASONING_PARAMETERS = (
    "thinking_effort",
    "reasoning_effort",
    "reasoning",
    "include_reasoning",
)

_OLLAMA_CLOUD_REASONING_FIELDS = ("reasoning_content", "reasoning")

_OLLAMA_CLOUD_REASONING_FIELD_DEFAULT = "reasoning_content"

_SHOW_DETAIL_CONCURRENCY = 8

_OPTION_KWARG_MAP = {
    "temperature": "temperature",
    "max_tokens": "num_predict",
    "top_p": "top_p",
}

_OLLAMA_TOOL_DONE_REASONS = frozenset({"tool_calls"})
