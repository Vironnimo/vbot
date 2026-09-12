"""Openai constants."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

CODEX_RESPONSES_MODE = "codex_responses"

CODEX_EXTRA_HEADERS: dict[str, str] = {
    "OpenAI-Beta": "responses=experimental",
    "originator": "vbot",
}

CODEX_WEBSOCKET_BETA = "responses_websockets=2026-02-06"

CODEX_RESPONSES_ENDPOINT = "/codex/responses"

RESPONSES_POLICY_ENDPOINT = "/responses"

OPENAI_METADATA_KEY = "openai"

OPENAI_WIRE_POLICIES_KEY = "wire_policies"

OPENAI_API_KEY_WIRE_KEY = "api-key"

OPENAI_SUBSCRIPTION_WIRE_KEY = "subscription"

OPENAI_RESPONSES_PROTOCOL = "responses"

OPENAI_PLATFORM_RESPONSES_REQUEST_PARAMETERS = frozenset(
    {"max_tokens", "max_output_tokens", "top_p"}
)

OPENAI_REASONING_CONTEXTS = frozenset({"auto", "current_turn", "all_turns"})

OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS = "You are a helpful assistant."

OPENAI_SUBSCRIPTION_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})

OPENAI_SUBSCRIPTION_REQUEST_PARAMETERS: frozenset[str] = frozenset()

OPTIONAL_REQUEST_PARAMETER_NAMES = frozenset(
    {"max_tokens", "max_output_tokens", "temperature", "top_p", "top_k", "stop_sequences"}
)

REASONING_PARAMETER_NAMES = frozenset(
    {"thinking_effort", "reasoning_effort", "reasoning", "include_reasoning"}
)

STRUCTURED_OUTPUT_PARAMETER_NAMES = frozenset(
    {"response_format", "structured_outputs", "json_mode"}
)

TOOL_PARAMETER_NAMES = frozenset({"tools", "tool_choice", "parallel_tool_calls"})

DISCOVERY_TOOL_PARAMETER_NAMES = frozenset({"tools", "tool_calls", "function_calling"})

DISCOVERY_JSON_PARAMETER_NAMES = frozenset({"response_format", "structured_outputs", "json_mode"})

DISCOVERY_REASONING_PARAMETER_NAMES = frozenset(
    {"reasoning", "reasoning_effort", "include_reasoning", "thinking_effort"}
)

CODEX_CLIENT_VERSION_FALLBACK = "0.144.0"

CODEX_PACKAGE_METADATA_URL = "https://registry.npmjs.org/@openai%2Fcodex/latest"

_CODEX_STABLE_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

CODEX_CACHE_SCOPE_HEADERS = ("session_id", "x-client-request-id")

CONVERSATION_ID_KWARG = "conversation_id"

PROMPT_CACHE_AFFINITY_ID_KWARG = "prompt_cache_affinity_id"

_NORMALIZED_CODEX_STREAM_RESPONSE_KEY = "_normalized_codex_stream_response"

_CODEX_TRANSPORT_AUTO: Literal["auto"] = "auto"

_CODEX_TRANSPORT_SSE: Literal["sse"] = "sse"

_CODEX_CACHE_SCOPE_MAX_LENGTH = 64

_CODEX_WEBSOCKET_CONNECT_TIMEOUT_SECONDS = 10.0

_CODEX_WEBSOCKET_STATUS_CODE = 101

CodexTransport = Literal["auto", "sse"]

CodexWebSocketConnector = Callable[..., Awaitable[Any]]

CodexWebSocketRoute = tuple[str, str, str]
