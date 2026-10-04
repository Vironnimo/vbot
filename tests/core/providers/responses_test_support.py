"""Helpers for the shared stateless Responses codec tests.

OpenAI, GitHub Copilot, OpenCode Go, OpenCode Zen, OpenRouter and xAI build and decode
``/responses`` traffic through the same codec (``github_copilot_responses.py``).
These helpers drive it directly, without an Adapter or HTTP transport.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from core.providers._responses_profile import ProfileResponsesPolicy
from core.providers.github_copilot_responses import (
    ResponsesRequestPolicy,
    ResponsesStreamState,
    iter_responses_sse_deltas_with_state,
)
from core.providers.reasoning_dialects import render_reasoning
from core.providers.wire_profile import ReasoningWire


def responses_policy(
    *,
    tool_calls: bool = True,
    parallel_tool_calls: bool = True,
    structured_outputs: bool = True,
) -> ResponsesRequestPolicy:
    """A request policy with every optional feature unless switched off.

    Its optional parameters are ``max_tokens``, ``max_output_tokens`` and
    ``top_p``.
    """

    return ProfileResponsesPolicy(
        supports_tools=tool_calls,
        supports_parallel_tool_calls=parallel_tool_calls,
        supports_structured_outputs=structured_outputs,
        supported_request_parameters=frozenset({"max_tokens", "max_output_tokens", "top_p"}),
    )


def responses_reasoning(
    effort: str | None = None,
    *,
    levels: Iterable[str] = ("low", "medium", "high", "xhigh"),
    supported: bool = True,
) -> Callable[[dict[str, Any]], None]:
    """Render ``effort`` in the ``responses_reasoning`` dialect for a Model with ``levels``.

    A reasoning Model (``supported``) also asks for encrypted continuity.
    """

    wire = ReasoningWire(
        dialect="responses_reasoning",
        supported=supported,
        control="levels",
        levels=tuple(levels),
    )
    intent = wire.plan(effort)
    return lambda payload: render_reasoning(wire, intent, payload)


def sse_event(event: str, data: Mapping[str, Any]) -> str:
    """One named SSE event; ``data`` may omit ``type`` to rely on the event name."""

    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def decode_responses_sse(
    lines: Iterable[str], state: ResponsesStreamState | None = None
) -> list[dict[str, Any]]:
    """Decode Responses SSE lines with a fresh (or the given) stream state."""

    return list(iter_responses_sse_deltas_with_state(lines, state or ResponsesStreamState()))
