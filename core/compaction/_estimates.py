"""Compaction's token counts, in the unit of the Session's Context.

Every count is what Chat counts for a request (``chat/usage.md`` -> One Context
rule): the selected wire's estimate, in which replayed Reasoning costs its
reported tokens, times the Model's input estimate factor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from core.chat.messages import ChatMessage, JsonObject
from core.chat.wire_shaping import history_request_messages
from core.compaction._tail_types import RequestTokenEstimator
from core.providers.adapter import estimate_wire_request_input_tokens
from core.providers.providers import RequestInput
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


class _ModelRequestInput(NamedTuple):
    """One Compaction Model request's input, counted once for its send and its response."""

    budget: RequestInput
    """The corrected input, for the Adapter's output limit and prompt-limit checks."""
    estimated: int
    """The uncorrected selected-wire estimate of exactly the sent messages and Tools."""


def _model_request_input_budget(
    adapter: Any,
    model_id: str,
    messages: Sequence[JsonObject],
    tools: Sequence[JsonObject] | None,
    *,
    factor: float,
    planned: Sequence[JsonObject] = (),
    active_request: Sequence[JsonObject] = (),
    active_context: RequestInput | None = None,
) -> _ModelRequestInput:
    """Return the input of one Compaction Model request: its budget and uncorrected estimate.

    *messages* is the request as sent and *planned* the Strategy's request
    before route shaping. When *planned* appends to the complete
    *active_request*, whose Context Chat knows as *active_context* (its
    measurement plus the corrected change), the budget is that Context plus
    the appended messages' corrected estimate, and only the estimated parts
    carry the Adapter's estimation margin. Otherwise the whole budget is the
    corrected estimate of the request. The uncorrected estimate is the
    calibration sample's base when the Provider measures the request's input.
    """

    def estimate(request: Sequence[JsonObject]) -> int:
        return estimate_wire_request_input_tokens(adapter, request, model_id=model_id, tools=tools)

    total = estimate(messages)
    known = len(active_request)
    appended = len(planned) - known
    if (
        active_context is None
        or known == 0
        or not 0 < appended < len(messages)
        or list(planned[:known]) != list(active_request)
    ):
        return _ModelRequestInput(RequestInput.estimate(round(total * factor)), total)
    added = round(max(0, total - estimate(messages[:-appended])) * factor)
    return _ModelRequestInput(
        RequestInput(active_context.tokens + added, True, active_context.estimated_tokens + added),
        total,
    )
