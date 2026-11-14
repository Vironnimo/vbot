"""Factory management schemas and bounded values shared by the page and CLI."""

from __future__ import annotations

from typing import Any

from ._limits import LIMIT_SCHEMA
from .store import FactoryError

type Json = dict[str, Any]

_STEP = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "kind": {"type": "string", "enum": ["agent", "workflow"]},
        "after": {"type": "array", "items": {"type": "string"}},
        "approval": {"type": "boolean"},
        "limits": LIMIT_SCHEMA,
        "source": {"type": "string", "enum": ["agent", "team"]},
        "agent": {"type": "string"},
        "model": {"type": "string"},
        "effort": {"type": "string"},
        "task": {"type": "string"},
        "returns_to": {"type": "string"},
        "rounds": {"type": "integer", "minimum": 1, "maximum": 20},
        "workflow": {"type": "string"},
        "wait": {"type": "boolean"},
    },
}
_TRIGGER = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "type": {"type": "string", "enum": ["schedule", "webhook"]},
        "schedule": {"type": "string"},
        "enabled": {"type": "boolean"},
        "project": {"type": "string"},
        "title": {"type": "string"},
        "task": {"type": "string"},
    },
}
WORKFLOW_SCHEMA: Json = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "enabled": {"type": "boolean"},
        "project_id": {"type": "string"},
        "final_approval": {"type": "boolean"},
        "limits": LIMIT_SCHEMA,
        "steps": {"type": "array", "items": _STEP},
        "triggers": {"type": "array", "items": _TRIGGER},
    },
}


def schema(properties: Json, required: tuple[str, ...] = ()) -> Json:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_ID = {"id": {"type": "string", "minLength": 1}}
_PROJECT = {"project_id": {"type": "string"}}
_REVISION = {"expected_revision": {"type": "integer", "minimum": 1}}
_STEP_CHOICE = {"step": {"type": "string"}}
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
        ("title", "description"),
    ),
    "card.update": schema(
        {**_ID, **_REVISION, "title": {"type": "string"}, "description": {"type": "string"}},
        ("id",),
    ),
    "card.limits": schema(
        {
            **_ID,
            **_REVISION,
            **_STEP_CHOICE,
            "card_limits": LIMIT_SCHEMA,
            "step_limits": LIMIT_SCHEMA,
        },
        ("id", "expected_revision"),
    ),
    "card.move": schema({**_ID, "position": {"type": "number"}}, ("id", "position")),
    "card.accept": schema({**_ID, **_REVISION, **_STEP_CHOICE}, ("id", "expected_revision")),
    "card.send_back": schema(
        {**_ID, **_STEP_CHOICE, "comment": {"type": "string"}}, ("id", "comment")
    ),
    "card.answer": schema({**_ID, **_STEP_CHOICE, "text": {"type": "string"}}, ("id", "text")),
    "card.retry": schema({**_ID, **_STEP_CHOICE}, ("id",)),
    **{
        f"card.{action}": schema(_ID, ("id",))
        for action in ("approve_proposal", "discard", "delete")
    },
    "workflow.list": schema({}),
    "workflow.get": schema(_ID, ("id",)),
    "workflow.save": schema(
        {"workflow": WORKFLOW_SCHEMA, "expected_revision": {"type": "integer", "minimum": 0}},
        ("workflow",),
    ),
    "workflow.preview": schema({"workflow": WORKFLOW_SCHEMA}, ("workflow",)),
    "workflow.delete": schema(_ID, ("id",)),
    "workflow.secret": schema(
        {**_ID, "trigger": {"type": "string", "minLength": 1}, "rotate": {"type": "boolean"}},
        ("id", "trigger"),
    ),
}
SECRET_OPERATIONS = frozenset({"workflow.secret"})

OPERATION_DESCRIPTIONS = {
    "board.get": "List the Board's Cards; project_id narrows it to one Project.",
    "card.create": "Create a queued Card. Omit workflow for the first enabled Workflow; omit "
    "project_id for a Card without a Project, or when the Workflow fixes its Project.",
    "card.limits": "Replace the Card or one step's execution limits of a waiting Card. "
    "Omit a scope to keep its limits; use {} to remove all limits in that scope; step_limits "
    "needs step. Consumption is retained. Resume with card.retry after a budget pause.",
    "card.accept": "Approve the waiting step named in step (or the first waiting one), overrule "
    "a step that kept sending work back, or accept a finished Card.",
    "card.send_back": "Rework the step named in step (default: the step a waiting step sends "
    "work back to, or the last step) with your comment; the steps after it run again.",
    "card.answer": "Answer the question of the step named in step (default: the first blocked "
    "step).",
    "card.retry": "Retry the failed, interrupted or paused step named in step (default: the first "
    "one), or a Card that could not start.",
    "workflow.list": "List the global Workflows with their triggers' state.",
    "workflow.get": "Read one Workflow and its steps' effective Models.",
    "workflow.save": "Create a Workflow (omit expected_revision or use 0) or replace a saved one "
    "(its id and expected_revision). Triggers are created, changed or removed with it.",
    "workflow.preview": "Resolve each Agent step's effective Model for an unsaved Workflow.",
    "workflow.delete": "Delete a Workflow and its triggers. Refused while another Workflow starts "
    "it or Cards wait to start with it.",
    "workflow.secret": "Read a webhook trigger's secret for the sender's settings; rotate "
    "replaces it first.",
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
