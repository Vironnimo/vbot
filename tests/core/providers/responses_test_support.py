"""Helpers for the shared stateless Responses codec tests.

OpenAI, GitHub Copilot, OpenCode Go, OpenRouter and xAI build and decode
``/responses`` traffic through the same codec (``github_copilot_responses.py``).
These helpers drive it directly, without an Adapter or HTTP transport.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from core.providers.github_copilot_policy import RESPONSES_ENDPOINT, copilot_model_policy
from core.providers.github_copilot_responses import (
    ResponsesRequestPolicy,
    ResponsesStreamState,
    iter_responses_sse_deltas_with_state,
)


def responses_policy(model_id: str = "gpt-5.4", **overrides: Any) -> ResponsesRequestPolicy:
    """A request policy with every optional feature unless overridden.

    The payload builder reads a Provider policy. The GitHub Copilot policy
    serves here because its catalog facts (``overrides``) switch each feature.
    """

    facts: dict[str, Any] = {
        "vendor": "OpenAI",
        "family": model_id,
        "version": model_id,
        "supported_endpoints": [RESPONSES_ENDPOINT],
        "reasoning_efforts": ["low", "medium", "high", "xhigh"],
        "tool_calls": True,
        "parallel_tool_calls": True,
        "streaming": True,
        "structured_outputs": True,
        **overrides,
    }
    return copilot_model_policy(model_id, {"github_copilot": facts})


def sse_event(event: str, data: Mapping[str, Any]) -> str:
    """One named SSE event; ``data`` may omit ``type`` to rely on the event name."""

    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def decode_responses_sse(
    lines: Iterable[str], state: ResponsesStreamState | None = None
) -> list[dict[str, Any]]:
    """Decode Responses SSE lines with a fresh (or the given) stream state."""

    return list(iter_responses_sse_deltas_with_state(lines, state or ResponsesStreamState()))
