"""Chat Completions wire and capability constants."""

from __future__ import annotations

SSE_DONE_MARKER = "[DONE]"

CHAT_COMPLETIONS_ENDPOINT = "/chat/completions"

OPENAI_REASONING_EFFORTS = {"low", "medium", "high"}

OPENAI_REASONING_KEYS = ("reasoning", "reasoning_content", "reasoning_text", "thinking")

OPENAI_REASONING_META_KEYS = ("encrypted_content", "reasoning_details")

_OPENAI_STREAM_REASONING_DETAILS_STATE_KEY = "openai_reasoning_details"

_OPENAI_STREAM_REASONING_FIELDS_STATE_KEY = "openai_reasoning_fields"
"""Stream state: the profile's readable reasoning fields, in priority order."""

_OPENAI_STREAM_REASONING_FIELD_SEEN_STATE_KEY = "openai_reasoning_field_seen"
"""Stream state: the field the stream's readable reasoning first arrived in."""

_OPENAI_TOOL_CALL_INDEX_IDS_STATE_KEY = "openai_tool_call_index_ids"

_OPENAI_TOOL_CALL_NAMES_STATE_KEY = "openai_tool_call_names"

OPENAI_TOOL_FINISH_REASONS = {"tool_calls", "function_call"}

# Besides ``length``, compatible APIs name a turn cut off by the Context window:
# Mistral ``model_length``, GLM (Z.ai) ``model_context_window_exceeded``.
OPENAI_TRUNCATED_FINISH_REASONS = frozenset(
    {"length", "model_length", "model_context_window_exceeded"}
)

# Besides ``content_filter``, GLM (Z.ai) names a turn its content-safety review
# blocked ``sensitive``.
OPENAI_CONTENT_FILTER_FINISH_REASONS = frozenset({"content_filter", "sensitive"})

OPENAI_ERROR_FINISH_REASONS = {
    "cancelled",
    "error",
    "failed",
    "server_error",
}

OPENAI_NATIVE_TRANSPORT_FINISH_REASONS = frozenset({"network_error", "server_error"})

DEFAULT_MAX_OUTPUT_TOKENS = 8192

# ``max_model_len`` is the window vLLM and SGLang serve, reported on each
# ``/v1/models`` entry (their ``ModelCard``).
CONTEXT_WINDOW_KEYS = ("context_length", "context_window", "contextWindow", "max_model_len")

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
