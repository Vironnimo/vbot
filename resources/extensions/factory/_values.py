"""Factory management schemas and bounded values shared by the page and CLI."""

from __future__ import annotations

from typing import Any

from .store import FactoryError

type Json = dict[str, Any]

PROFILE_SOURCES = ("agent", "team")
SETUP_DEFAULTS: Json = {
    "enabled": True,
    "worker_source": "agent",
    "worker_profile": "",
    "worker_model": "",
    "worker_effort": "",
    "reviewer_source": "agent",
    "reviewer_profile": "",
    "reviewer_model": "",
    "reviewer_effort": "",
    "review_rounds": 3,
    "turn_budget": 30,
    "base_branch": "main",
    "integration_check": "merged",
}
SETUP_PROPERTIES: Json = {
    "project_id": {"type": "string", "minLength": 1},
    "enabled": {"type": "boolean"},
    "worker_source": {"type": "string", "enum": list(PROFILE_SOURCES)},
    "reviewer_source": {"type": "string", "enum": list(PROFILE_SOURCES)},
    **{
        field: {"type": "string"}
        for field in (
            "worker_profile",
            "worker_model",
            "worker_effort",
            "reviewer_profile",
            "reviewer_model",
            "reviewer_effort",
            "base_branch",
        )
    },
    "review_rounds": {"type": "integer", "minimum": 1, "maximum": 20},
    "turn_budget": {"type": "integer", "minimum": 1, "maximum": 1000},
    "integration_check": {"type": "string", "enum": ["merged", "reference_only"]},
    "expected_revision": {"type": "integer", "minimum": 0},
}


def participant_profile(setup: Json, role: str) -> Json:
    """The ``TemporaryAgentConfig`` Profile fields of one role's chosen Agent.

    Card snapshots saved before ``*_source`` existed name a Team Agent.
    """
    profile = setup[f"{role}_profile"]
    if setup.get(f"{role}_source", "team") == "agent":
        return {"identity_profile": profile}
    return {"repository_profile": profile}


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
        {**_PROJECT, "title": {"type": "string"}, "description": {"type": "string"}},
        ("project_id", "title", "description"),
    ),
    "card.update": schema(
        {**_ID, **_REVISION, "title": {"type": "string"}, "description": {"type": "string"}},
        ("id",),
    ),
    "card.move": schema({**_ID, "position": {"type": "number"}}, ("id", "position")),
    "card.accept": schema({**_ID, **_REVISION}, ("id", "expected_revision")),
    "card.send_back": schema({**_ID, "comment": {"type": "string"}}, ("id", "comment")),
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
