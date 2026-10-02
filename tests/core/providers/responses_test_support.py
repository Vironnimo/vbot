"""Helpers for the shared stateless Responses codec tests.

OpenAI, GitHub Copilot, OpenCode Go, OpenRouter and xAI build and decode
``/responses`` traffic through the same codec (``github_copilot_responses.py``).
These helpers drive it directly, without an Adapter or HTTP transport.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from core.providers._openai_constants import OPENAI_PLATFORM_RESPONSES_REQUEST_PARAMETERS
from core.providers._openai_policy import OpenAISubscriptionResponsesPolicy
from core.providers.github_copilot_responses import (
    ResponsesRequestPolicy,
    ResponsesStreamState,
    iter_responses_sse_deltas_with_state,
)


def responses_policy(
    *,
    reasoning_efforts: Iterable[str] = ("low", "medium", "high", "xhigh"),
    tool_calls: bool = True,
    parallel_tool_calls: bool = True,
    structured_outputs: bool = True,
) -> ResponsesRequestPolicy:
    """A request policy with every optional feature unless switched off.

    The payload builder reads a Provider policy that derives reasoning from the
    caller's effort. The OpenAI declared-Responses policy serves here because
    its fields switch each feature; its optional parameters are ``max_tokens``,
    ``max_output_tokens`` and ``top_p``.
    """

    return OpenAISubscriptionResponsesPolicy(
        allowed_reasoning_efforts=frozenset(reasoning_efforts),
        supports_tools=tool_calls,
        supports_parallel_tool_calls=parallel_tool_calls,
        supports_structured_outputs=structured_outputs,
        supported_request_parameters=OPENAI_PLATFORM_RESPONSES_REQUEST_PARAMETERS,
    )


def sse_event(event: str, data: Mapping[str, Any]) -> str:
    """One named SSE event; ``data`` may omit ``type`` to rely on the event name."""

    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def decode_responses_sse(
    lines: Iterable[str], state: ResponsesStreamState | None = None
) -> list[dict[str, Any]]:
    """Decode Responses SSE lines with a fresh (or the given) stream state."""

    return list(iter_responses_sse_deltas_with_state(lines, state or ResponsesStreamState()))
