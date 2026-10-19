"""Compaction's token counts, in the unit of the Session's Context.

Every count is what Chat counts for a request (``chat/usage.md`` -> One Context
rule): the selected wire's estimate, in which replayed Reasoning costs its
reported tokens, times the Model's input estimate factor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.chat.messages import ChatMessage, JsonObject
from core.chat.wire_shaping import history_request_messages
from core.compaction._tail_types import RequestTokenEstimator
from core.providers.adapter import estimate_wire_request_input_tokens
from core.utils.tokens import estimate_request_input_tokens


def _request_estimator(
    adapter: Any | None, model_id: str | None, factor: float
) -> RequestTokenEstimator:
    """Count request messages as *adapter* sends them to *model_id*, corrected by *factor*.

    Without a route, the shared Chat Completions estimate stands in for the wire.
    """

    def estimate(request: Sequence[Mapping[str, Any]]) -> int:
        if adapter is None or model_id is None:
            return round(estimate_request_input_tokens(request)[0] * factor)
        return round(
            estimate_wire_request_input_tokens(adapter, request, model_id=model_id) * factor
        )

    return estimate


def _history_tokens(
    history: Sequence[ChatMessage],
    request_messages: Sequence[JsonObject],
    estimate: RequestTokenEstimator,
) -> int:
    """Count *history* as the live request sends it (``history_request_messages``)."""
    if not history:
        return 0
    return estimate(history_request_messages(history, request_messages))
