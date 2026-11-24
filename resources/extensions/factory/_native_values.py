"""Flat management contracts for permanent Factory teams and conversations."""

from __future__ import annotations

from typing import Any

type Json = dict[str, Any]
S = {"type": "string"}
INTEGER = {"type": "integer", "minimum": 1}
A = {"type": "array", "items": S}
OBJECT = {"type": "object"}
CONFIG = {
    "type": "object",
    "properties": {
        "allowed_models": A,
        "allowed_skills": A,
        "allowed_tools": A,
        "denied_tools": A,
        "permission_ceiling_profile": S,
        "permission_ceiling": {"type": "object"},
    },
}
LOCAL = {
    "allowed_skills": A,
    "allowed_tools": A,
    "denied_tools": A,
}
B = {"type": "boolean"}
PAGING = {"after": S, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}
FACTORY = {
    "name": S,
    "remit": S,
    "capacity": INTEGER,
    "config": CONFIG,
}
MEMBER = {"role": S, "instructions": S, "config": CONFIG, "capacity": INTEGER, **LOCAL}


def schema(properties: Json, required: tuple[str, ...] = ()) -> Json:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


ID = {"id": S}
REV = {**ID, "expected_revision": INTEGER}
CONVERSATION = {"project_id": S, "member_id": S, "topic_id": S}
OPERATION_SCHEMAS = {
    "agent.profile_targets": schema({"scope": S, "after": S}, ("scope",)),
    "conversation.get": schema({**CONVERSATION, "before": S, "new_topic": B}, ("project_id",)),
    "conversation.stop": schema({"project_id": S, "topic_id": S}, ("project_id", "topic_id")),
    "conversation.send": schema(
        {**CONVERSATION, "text": S, "request_id": S, "new_topic": B},
        ("project_id", "text", "request_id"),
    ),
    "factory.list": schema({"project_id": S, **PAGING}),
    "factory.get": schema(ID, ("id",)),
    "factory.create": schema(
        {
            "project_id": S,
            "leader_agent_id": S,
            "request_id": S,
            "leader_name": S,
            "leader_model": S,
            **FACTORY,
        },
        ("project_id",),
    ),
    "factory.update": schema(
        {
            **REV,
            **FACTORY,
            "leader_member_id": S,
            "state": S,
            "clear_fields": {
                "type": "array",
                "items": {"type": "string", "enum": list(CONFIG["properties"])},
            },
        },
        ("id", "expected_revision"),
    ),
    "factory.archive": schema(REV, ("id", "expected_revision")),
    "member.list": schema({"factory_id": S, **PAGING}, ("factory_id",)),
    "member.add": schema({"factory_id": S, "agent_id": S, **MEMBER}, ("factory_id", "agent_id")),
    "member.create": schema(
        {"factory_id": S, "agent_id": S, "request_id": S, "name": S, "model": S, **MEMBER},
        ("factory_id", "name", "request_id"),
    ),
    "member.update": schema(
        {
            **REV,
            **MEMBER,
            "clear_fields": {"type": "array", "items": {"type": "string", "enum": list(LOCAL)}},
        },
        ("id", "expected_revision"),
    ),
    "member.retire": schema(REV, ("id", "expected_revision")),
}
OPERATION_DESCRIPTIONS = {
    "factory.create": (
        "Create a permanent Project Factory with an existing Leader, or create "
        "its Leader with leader_name and leader_model."
    ),
    "factory.update": (
        "Change only supplied Factory settings; expected_revision comes from factory.get."
    ),
    "member.add": "Add an existing Identity Agent without changing its global configuration.",
    "member.create": "Create a durable Identity Agent and add it to the Factory.",
    "member.update": "Change the member's role, instructions, capacity or access in this Factory.",
}
