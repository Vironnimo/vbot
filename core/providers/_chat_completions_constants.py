"""Chat Completions wire and capability constants."""

from __future__ import annotations

SSE_DONE_MARKER = "[DONE]"

CHAT_COMPLETIONS_ENDPOINT = "/chat/completions"

OPENAI_REASONING_EFFORTS = {"low", "medium", "high"}

OPENAI_REASONING_EFFORTS_WITH_NONE = {"none", *OPENAI_REASONING_EFFORTS}

OPENAI_NONE_REASONING_PROVIDER_IDS = {"openai"}

OPENAI_REASONING_KEYS = ("reasoning", "reasoning_content", "reasoning_text", "thinking")

OPENAI_REASONING_META_KEYS = ("encrypted_content", "reasoning_details")

_OPENAI_STREAM_REASONING_DETAILS_STATE_KEY = "openai_reasoning_details"

_OPENAI_TOOL_CALL_INDEX_IDS_STATE_KEY = "openai_tool_call_index_ids"

REASONING_RESPONSE_FIELD_METADATA_KEY = "reasoning_response_field"

OPENAI_TOOL_FINISH_REASONS = {"tool_calls", "function_call"}

OPENAI_ERROR_FINISH_REASONS = {
    "cancelled",
    "error",
    "failed",
    "network_error",
    "server_error",
}

OPENAI_NATIVE_TRANSPORT_FINISH_REASONS = frozenset({"network_error", "server_error"})

DEFAULT_MAX_OUTPUT_TOKENS = 8192

CONTEXT_WINDOW_KEYS = ("context_length", "context_window", "contextWindow")

MAX_OUTPUT_TOKEN_KEYS = (
    "max_output_tokens",
    "max_completion_tokens",
    "maxOutputTokens",
    "maxCompletionTokens",
)

JSON_MODE_PARAMETER_NAMES = {"response_format", "structured_outputs", "json_mode"}

REASONING_PARAMETER_NAMES = {"reasoning", "include_reasoning", "reasoning_effort"}

OUTPUT_LIMIT_PARAMETER_NAMES = ("max_tokens", "max_completion_tokens", "max_output_tokens")

_OPENAI_INPUT_AUDIO_FORMATS = {
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
}
