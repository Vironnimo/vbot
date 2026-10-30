"""One native Decision target's execution contract and selection facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.model_tasks.decision_pricing import pricing_for_decision
from core.model_tasks.decision_types import DecisionError
from core.models.pricing import TokenPricing
from core.providers.wire_profile import DecisionWire


@dataclass(frozen=True)
class DecisionProfile:
    """A request-local snapshot; limits are facts, never generation controls."""

    wire: DecisionWire
    max_input_tokens: int | None = None
    max_questions: int | None = None
    max_choices: int | None = None
    max_levels: int | None = None
    max_request_bytes: int | None = None
    max_text_bytes: int | None = None
    pricing: TokenPricing | None = None

    @property
    def supported(self) -> bool:
        return self.wire.protocol != "none" and bool(self.wire.endpoint)

    def validate_questions(self, questions: list[dict[str, Any]]) -> None:
        if self.max_questions is not None and len(questions) > self.max_questions:
            raise DecisionError(
                f"This Decision Model accepts at most {self.max_questions} questions per item. "
                "Nothing was sent. Split the questions into separate calls."
            )
        for question in questions:
            maximum = self.max_choices if question["type"] == "choice" else self.max_levels
            if (
                question["type"] != "noul"
                and maximum is not None
                and len(question["criteria"]) > maximum
            ):
                raise DecisionError(
                    f"Question {question['id']!r} exceeds this Decision Model's limit of "
                    f"{maximum} {'options' if question['type'] == 'choice' else 'levels'}. "
                    "Nothing was sent. Reduce the criteria."
                )


def decision_profile(model: Any | None, wire: DecisionWire, connection_id: str) -> DecisionProfile:
    """Combine the selected native route with published Model limits."""
    capabilities = getattr(model, "capabilities", None)
    tasks = getattr(capabilities, "task_options", {})
    facts = tasks.get("decision", {}) if isinstance(tasks, Mapping) else {}
    limits = facts.get("limits", {}) if isinstance(facts, Mapping) else {}

    def limit(name: str) -> int | None:
        value = limits.get(name) if isinstance(limits, Mapping) else None
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
        )

    window = limit("max_input_tokens")
    # A Chat Model's Context is not evidence for its separate Decisions endpoint.
    if window is None and "decisions" in getattr(capabilities, "output_modalities", ()):
        getter = getattr(model, "context_window_for", None)
        reported = (
            getter(connection_id) if callable(getter) else getattr(model, "context_window", None)
        )
        if isinstance(reported, int) and not isinstance(reported, bool) and reported > 0:
            window = reported
    return DecisionProfile(
        wire=wire,
        max_input_tokens=window,
        max_questions=limit("max_questions"),
        max_choices=limit("max_choices"),
        max_levels=limit("max_levels"),
        max_request_bytes=limit("max_request_bytes"),
        max_text_bytes=limit("max_text_bytes"),
        pricing=pricing_for_decision(model),
    )
