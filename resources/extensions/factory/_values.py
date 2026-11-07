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
}
_STEP = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "source": {"type": "string", "enum": ["agent", "team"]},
        "agent": {"type": "string"},
        "model": {"type": "string"},
        "effort": {"type": "string"},
        "task": {"type": "string"},
        "after": {"type": "array", "items": {"type": "string"}},
        "approval": {"type": "boolean"},
        "returns_to": {"type": "string"},
        "rounds": {"type": "integer", "minimum": 1, "maximum": 20},
    },
}
_WORKFLOW = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "steps": {"type": "array", "items": _STEP},
        "sources": {"type": "array", "items": {"type": "object"}},
    },
}
SETUP_PROPERTIES: Json = {
    "project_id": {"type": "string", "minLength": 1},
    "enabled": {"type": "boolean"},
    "workflows": {
        "type": "array",
        "items": _WORKFLOW,
    },
    "default_workflow": {"type": "string"},
    "turn_budget": {"type": "integer", "minimum": 1, "maximum": 1000},
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
_VERSION = {"version": {"type": "integer", "minimum": 1}}
_UPDATE = {
    "workflow": _WORKFLOW,
    "source_id": {"type": "string", "minLength": 1},
    **_VERSION,
    "resolutions": {
        "type": "object",
        "additionalProperties": {"type": "string", "enum": ["local", "template"]},
    },
}
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
        {**_ID, "comment": {"type": "string"}, "step": {"type": "string"}}, ("id", "comment")
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
    "library.list": schema(
        {
            "include_archived": {"type": "boolean"},
            "after": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
        }
    ),
    "library.get": schema({**_ID, **_VERSION}, ("id",)),
    "library.publish": schema(
        {
            **_ID,
            "expected_revision": {"type": "integer", "minimum": 0},
            "name": {"type": "string", "minLength": 1, "maxLength": 80},
            "description": {"type": "string", "maxLength": 2000},
            "kind": {"type": "string", "enum": ["step", "sequence", "workflow"]},
            "workflow": _WORKFLOW,
        },
        ("name", "kind", "workflow"),
    ),
    "library.archive": schema(
        {**_ID, **_REVISION, "archived": {"type": "boolean"}}, ("id", "expected_revision")
    ),
    "library.extract": schema(
        {
            "workflow": _WORKFLOW,
            "step_ids": {"type": "array", "items": {"type": "string"}},
            "source_id": {"type": "string"},
        },
        ("workflow",),
    ),
    "library.insert": schema(
        {
            **_ID,
            **_VERSION,
            "workflow": _WORKFLOW,
            "after": {"type": "array", "items": {"type": "string"}},
        },
        ("id",),
    ),
    "library.link": schema(
        {
            **_ID,
            **_VERSION,
            "workflow": _WORKFLOW,
            "step_ids": {"type": "array", "items": {"type": "string"}},
            "replace_sources": {"type": "boolean"},
        },
        ("id", "workflow"),
    ),
    "library.preview_update": schema(_UPDATE, ("workflow", "source_id")),
    "library.apply_update": schema(_UPDATE, ("workflow", "source_id", "version")),
}

OPERATION_DESCRIPTIONS = {
    "library.list": "List shared templates in id order. Omit include_archived to hide archived "
    "templates; continue with next_cursor as after.",
    "library.get": "Read a template and its version numbers. Omit version for the latest version.",
    "library.publish": "Publish an immutable template version. Omit id for a new template; "
    "for an existing template supply its expected_revision. Project copies stay unchanged.",
    "library.archive": "Archive a template while retaining its versions and Project copies. "
    "Set archived to false to restore it.",
    "library.extract": "Prepare a template draft from a Workflow or selected step_ids. "
    "Supply source_id to preserve the original template's step identities when publishing "
    "another version. Nothing is saved.",
    "library.insert": "Insert a linked template into a Workflow draft. Omit workflow to create "
    "a Workflow; omit after to append, or pass [] to insert at the beginning. Nothing is saved.",
    "library.link": "Link existing steps to the template published from them without inserting "
    "copies. Omit step_ids for the whole Workflow. Set replace_sources to true to replace "
    "existing links fully covered by the selection; partial overlaps are refused. "
    "Returns a draft; nothing is saved.",
    "library.preview_update": "Preview adopting a template version in a Workflow draft. "
    "Omit version for latest; choose local or template for each conflict id in resolutions.",
    "library.apply_update": "Resolve adoption of the exact previewed version, refusing unresolved "
    "conflicts. Returns a Workflow draft; save it with setup.update and its expected_revision.",
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
