"""Flat management contracts for permanent Factory teams and work."""

from __future__ import annotations

from typing import Any

from ._limits import LIMIT_SCHEMA

type Json = dict[str, Any]
S = {"type": "string"}
INTEGER = {"type": "integer", "minimum": 1}
A = {"type": "array", "items": S}
OBJECT = {"type": "object"}
CONFIG = {
    "type": "object",
    "properties": {
        "model": S,
        "thinking_effort": S,
        "allowed_models": A,
        "allowed_skills": A,
        "allowed_tools": A,
        "denied_tools": A,
        "permission_ceiling": {"type": "object"},
    },
}
LOCAL = {
    "model": S,
    "thinking_effort": S,
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
    "limits": LIMIT_SCHEMA,
}
MEMBER = {"role": S, "instructions": S, "config": CONFIG, "capacity": INTEGER, **LOCAL}
TASK = {
    "title": S,
    "objective": S,
    "conditions": S,
    "assignee_id": S,
    "parent_id": S,
    "dependencies": A,
    "priority": {"type": "integer"},
    "limits": LIMIT_SCHEMA,
    "workflow_id": S,
    "reviewer_id": S,
    "user_approval": B,
}


def schema(properties: Json, required: tuple[str, ...] = ()) -> Json:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


ID = {"id": S}
REV = {**ID, "expected_revision": INTEGER}
CONVERSATION = {"project_id": S, "member_id": S, "topic_id": S, "task_id": S, "card_id": S}
OPERATION_SCHEMAS = {
    "work.list": schema({"project_id": S, "query": S, **PAGING}, ("project_id",)),
    "conversation.get": schema({**CONVERSATION, "before": S}, ("project_id",)),
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
            "identity_config": {
                "type": "object",
                "properties": {"name": S, "model": S, "thinking_effort": S},
            },
        },
        ("id", "expected_revision"),
    ),
    "member.retire": schema(REV, ("id", "expected_revision")),
    "task.history": schema({"factory_id": S, "query": S, **PAGING}, ("factory_id",)),
    "task.list": schema(
        {"factory_id": S, "state": S, "assignee_id": S, "parent_id": S, **PAGING}, ("factory_id",)
    ),
    "task.get": schema(
        {
            **ID,
            "events_after": {"type": "integer", "minimum": 0},
            "results_after": {"type": "integer", "minimum": 0},
            "limit": PAGING["limit"],
        },
        ("id",),
    ),
    "task.create": schema({"factory_id": S, **TASK}, ("factory_id", "title", "objective")),
    "task.update": schema({**REV, **TASK}, ("id", "expected_revision")),
    "task.assign": schema({**REV, "assignee_id": S}, ("id", "expected_revision", "assignee_id")),
    **{
        f"task.{action}": schema(REV, ("id", "expected_revision"))
        for action in ("pause", "resume", "cancel", "reopen")
    },
    "task.complete": schema(
        {**REV, "outcome": S, "evidence": S}, ("id", "expected_revision", "outcome", "evidence")
    ),
    "task.review": schema(
        {**REV, "approve": B, "findings": S}, ("id", "expected_revision", "approve")
    ),
    "topic.list": schema({"factory_id": S, "task_id": S, **PAGING}, ("factory_id",)),
    "topic.get": schema({**ID, **PAGING, "after": {"type": "integer", "minimum": 0}}, ("id",)),
    "topic.create": schema(
        {"factory_id": S, "title": S, "task_id": S, "member_ids": A, "text": S},
        ("factory_id", "title"),
    ),
    "topic.invite": schema(
        {**REV, "member_ids": A, "text": S}, ("id", "expected_revision", "member_ids")
    ),
    "topic.post": schema({**ID, "text": S, "recipients": A, "source_receipt": S}, ("id", "text")),
    "topic.close": schema(REV, ("id", "expected_revision")),
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
    "member.update": (
        "Change Factory-local member settings; identity_config explicitly edits a"
        " Factory-created Identity Agent."
    ),
    "task.history": (
        "Find Project tasks and Workflow runs by title. "
        "Use get with a returned id to read its result."
    ),
    "task.create": (
        "Create native work; assigning a member starts eligible work "
        "automatically, without requiring a Workflow."
    ),
    "task.complete": (
        "Submit an outcome and evidence for the completion conditions; configured"
        " review remains pending."
    ),
    "task.review": "Approve the submitted result or request changes with findings.",
    "topic.post": (
        "Post attributed discussion content. For member posts, explicit recipients "
        "deliver to those members; omission records a shared reply without starting "
        "peer replies. User posts reach participants, and discussion answers also "
        "reach the linked task owner."
    ),
}
