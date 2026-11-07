"""Factory management schemas and bounded values shared by the page and CLI."""

from __future__ import annotations

from typing import Any

from .store import FactoryError

type Json = dict[str, Any]

SETUP_DEFAULTS: Json = {
    "enabled": True,
    "workflows": [],
    "default_workflow": "",
    "turn_budget": 30,
    "base_branch": "main",
    "integration_check": "merged",
}
_LANE = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "enum": ["agent", "team"]},
        "agent": {"type": "string"},
        "model": {"type": "string"},
        "effort": {"type": "string"},
    },
}
_STAGE = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "kind": {"type": "string", "enum": ["work", "review"]},
        "output": {"type": "string", "enum": ["changes", "document"]},
        "instructions": {"type": "string"},
        "lanes": {"type": "array", "items": _LANE},
        "approval": {"type": "boolean"},
        "target": {"type": "string"},
        "rounds": {"type": "integer", "minimum": 1, "maximum": 20},
    },
}
SETUP_PROPERTIES: Json = {
    "project_id": {"type": "string", "minLength": 1},
    "enabled": {"type": "boolean"},
    "workflows": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "stages": {"type": "array", "items": _STAGE},
            },
        },
    },
    "default_workflow": {"type": "string"},
    "base_branch": {"type": "string"},
    "turn_budget": {"type": "integer", "minimum": 1, "maximum": 1000},
    "integration_check": {"type": "string", "enum": ["merged", "reference_only"]},
    "expected_revision": {"type": "integer", "minimum": 0},
}


def schema(properties: Json, required: tuple[str, ...] = ()) -> Json:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_ID = {"id": {"type": "string", "minLength": 1}}
_PROJECT = {"project_id": {"type": "string", "minLength": 1}}
_REVISION = {"expected_revision": {"type": "integer", "minimum": 1}}
OPERATION_SCHEMAS: dict[str, Json] = {
    "catalog": schema({}),
    "board.get": schema(_PROJECT),
    "card.get": schema(_ID, ("id",)),
    "card.create": schema(
        {
            **_PROJECT,
            "title": {"type": "string"},
            "description": {"type": "string"},
            "workflow": {"type": "string"},
        },
        ("project_id", "title", "description"),
    ),
    "card.update": schema(
        {**_ID, **_REVISION, "title": {"type": "string"}, "description": {"type": "string"}},
        ("id",),
    ),
    "card.move": schema({**_ID, "position": {"type": "number"}}, ("id", "position")),
    "card.accept": schema({**_ID, **_REVISION}, ("id", "expected_revision")),
    "card.send_back": schema(
        {**_ID, "comment": {"type": "string"}, "stage": {"type": "string"}}, ("id", "comment")
    ),
    "card.answer": schema({**_ID, "text": {"type": "string"}}, ("id", "text")),
    **{
        f"card.{action}": schema(_ID, ("id",))
        for action in (
            "approve_proposal",
            "discard",
            "delete",
            "retry",
        )
    },
    "setup.list": schema({}),
    "setup.get": schema(_PROJECT, ("project_id",)),
    "setup.update": schema(SETUP_PROPERTIES, ("project_id",)),
    "setup.preview": schema(SETUP_PROPERTIES, ("project_id",)),
    "setup.delete": schema(_PROJECT, ("project_id",)),
}


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
