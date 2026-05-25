"""Shared reasoning-effort normalization helpers for provider adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterable, MutableMapping
from typing import Any

THINKING_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
THINKING_EFFORT_RANKS = {effort: rank for rank, effort in enumerate(THINKING_EFFORT_ORDER)}


def normalize_thinking_effort(value: Any) -> str:
    """Return a canonical vBot thinking effort or an empty string."""

    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower()
    return normalized if normalized in THINKING_EFFORT_RANKS else ""


def closest_supported_effort(value: Any, supported_efforts: Iterable[str]) -> str | None:
    """Map a vBot thinking effort to the nearest provider-supported effort.

    If two provider levels are equally close, the lower level wins so vBot does
    not silently increase reasoning cost beyond the selected level.
    """

    effort = normalize_thinking_effort(value)
    if not effort:
        return None

    supported = tuple(
        dict.fromkeys(
            supported_effort
            for raw_effort in supported_efforts
            if (supported_effort := normalize_thinking_effort(raw_effort))
        )
    )
    if effort == "none":
        return "none" if "none" in supported else None

    active_supported = tuple(
        supported_effort for supported_effort in supported if supported_effort != "none"
    )
    if not active_supported:
        return None
    if effort in active_supported:
        return effort

    target_rank = THINKING_EFFORT_RANKS[effort]
    return min(
        active_supported,
        key=lambda supported_effort: (
            abs(THINKING_EFFORT_RANKS[supported_effort] - target_rank),
            THINKING_EFFORT_RANKS[supported_effort],
        ),
    )


def model_reasoning_supported(
    model_lookup: Callable[[str], Any] | None,
    model_id: str,
) -> bool | None:
    """Return catalog reasoning support for a provider-local model when known."""

    if model_lookup is None:
        return None

    catalog_model_id = model_id.split("::", 1)[0]
    model = model_lookup(catalog_model_id)
    if model is None:
        return None
    supported = model.capabilities.reasoning.supported
    return supported if isinstance(supported, bool) else None


def remove_reasoning_kwargs(
    kwargs: MutableMapping[str, Any],
    *parameter_names: str,
) -> None:
    """Remove provider reasoning controls from a mutable request kwargs map."""

    for parameter_name in parameter_names:
        kwargs.pop(parameter_name, None)
