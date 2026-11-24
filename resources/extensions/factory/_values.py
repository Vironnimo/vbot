"""Shared bounded text and state validation helpers for Factory."""

from __future__ import annotations

from typing import Any

from .store import FactoryError

type Json = dict[str, Any]


def text(arguments: Json, field: str, *, maximum: int = 20000) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip():
        raise FactoryError(
            "missing_field", f"{field} needs a non-empty text value. Supply {field} and try again."
        )
    if len(value) > maximum:
        raise FactoryError(
            "text_too_long", f"{field} exceeds {maximum} characters. Shorten it and try again."
        )
    return value.strip() if field in {"title", "id", "project", "project_id"} else value


def require_state(card: Json, states: set[str] | frozenset[str]) -> None:
    if card["state"] not in states:
        allowed = ", ".join(sorted(states))
        raise FactoryError(
            "invalid_state",
            f"Card {card['id']} is {card['state']}. This action needs state {allowed}. "
            "Refresh the Card to choose an available action.",
        )
