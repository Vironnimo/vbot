"""The public declaration contract accepts only bounded UI conditions."""

from typing import Any

import pytest

from core.extensions.contributions import parse_contribution


@pytest.mark.parametrize(
    "condition",
    [
        {"message_role": "assistant"},
        {
            "all": [
                {"field": "context_tokens", "op": "gte", "value": 100},
                {"not": {"has_draft": True}},
            ]
        },
        {
            "any": [
                {"field": "editor_mode", "op": "in", "value": ["create", "edit"]},
                {"field": "session_id", "op": "exists", "value": False},
            ]
        },
    ],
)
def test_conditions_are_detached_and_accepted(condition: dict[str, Any]) -> None:
    declaration = parse_contribution(
        "example", surface="chat.message", kind="status", title="Example", when=condition
    )
    assert declaration.spec["when"] == condition
    condition.clear()
    assert declaration.spec["when"]


@pytest.mark.parametrize(
    "condition",
    [
        {"all": []},
        {"any": []},
        {"not": []},
        {"field": [], "op": "eq", "value": 1},
        {"field": "has_draft", "op": [], "value": 1},
        {"field": "password", "op": "eq", "value": "hidden"},
        {"field": "context_tokens", "op": "gt", "value": True},
        {"field": "context_tokens", "op": "gt", "value": float("inf")},
        {"field": "has_session", "op": "exists", "value": 1},
        {"field": "message_role", "op": "in", "value": "assistant"},
        {"any": [{"view": "chat"}] * 64},
    ],
)
def test_invalid_or_unbounded_conditions_are_rejected(condition: Any) -> None:
    with pytest.raises(ValueError):
        parse_contribution(
            "example", surface="shell.status", kind="status", title="Example", when=condition
        )
