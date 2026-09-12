"""Shared fixtures and fakes for github copilot responses behavior tests."""

from __future__ import annotations

import json

from core.providers.github_copilot_policy import RESPONSES_ENDPOINT, copilot_model_policy
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    iter_responses_sse_deltas_with_state,
)


def _iter_deltas(lines):
    """Parse Responses SSE lines with a fresh stream state (test convenience)."""
    return iter_responses_sse_deltas_with_state(lines, ResponsesStreamState())


def responses_policy(model_id: str = "gpt-5.4", **overrides):
    metadata = {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": model_id,
            "version": model_id,
            "supported_endpoints": [RESPONSES_ENDPOINT],
            "reasoning_efforts": ["low", "medium", "high", "xhigh"],
            "tool_calls": True,
            "parallel_tool_calls": True,
            "streaming": True,
            "structured_outputs": True,
        }
    }
    metadata["github_copilot"].update(overrides)
    return copilot_model_policy(model_id, metadata)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
