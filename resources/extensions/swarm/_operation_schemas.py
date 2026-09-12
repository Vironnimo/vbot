"""Operation schemas."""

from __future__ import annotations

from ._extension_values import (
    Json,
)

_PAGE = {
    "type": "object",
    "properties": {
        "cursor": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "additionalProperties": False,
}

_PROFILE_INPUT = {
    "type": "object",
    "description": (
        "Complete profile; get the current profile before replacement. Choose Model, Tool, "
        "Skill, and Project ids from catalog. For creation, replace the example's Model and "
        "absolute server directory. working_directory can instead be "
        "{kind: project, project_id: <id>}. Formation rows also accept thinking_effort, "
        "temperature, and fallback_models. Optional profile fields: slug, tools (per-Tool "
        "settings), allowed_skills, instructions, prompt_blocks, reminders, and delivery. "
        "catalog returns prompt_defaults and reminder_texts; use those to compose instructions. "
        "Save returns the full normalized profile with defaults and revision. Updating "
        "preserves its id and uses that revision as expected_revision; creation uses null."
    ),
    "examples": [
        {
            "schema_version": 1,
            "name": "Review",
            "participants": [{"model": "<model-id-from-catalog>", "count": 2}],
            "working_directory": {"kind": "directory", "path": "<absolute-server-directory>"},
            "tool_access": {"mode": "selected", "allowed": []},
        }
    ],
}

_OPERATION_SCHEMAS: dict[str, Json] = {
    "profiles.preview": {
        "type": "object",
        "properties": {
            "profile": _PROFILE_INPUT,
            "formation_index": {"type": "integer", "minimum": 0},
        },
        "required": ["profile"],
        "additionalProperties": False,
    },
    "catalog": {"type": "object", "properties": {}, "additionalProperties": False},
    "profiles.list": _PAGE,
    "profiles.get": {
        "type": "object",
        "properties": {"profile_id": {"type": "string", "minLength": 1}},
        "required": ["profile_id"],
        "additionalProperties": False,
    },
    "profiles.save": {
        "type": "object",
        "properties": {
            "profile": _PROFILE_INPUT,
            "expected_revision": {"type": ["integer", "null"], "minimum": 1},
        },
        "required": ["profile", "expected_revision"],
        "additionalProperties": False,
    },
    "profiles.delete": {
        "type": "object",
        "properties": {
            "profile_id": {"type": "string", "minLength": 1},
            "expected_revision": {"type": "integer", "minimum": 1},
        },
        "required": ["profile_id", "expected_revision"],
        "additionalProperties": False,
    },
    "swarms.list": _PAGE,
    "swarms.get": {
        "type": "object",
        "properties": {"swarm_id": {"type": "string", "minLength": 1}},
        "required": ["swarm_id"],
        "additionalProperties": False,
    },
    "swarms.delete": {
        "type": "object",
        "properties": {"swarm_id": {"type": "string", "minLength": 1}},
        "required": ["swarm_id"],
        "additionalProperties": False,
    },
    "swarms.events": {
        "type": "object",
        "properties": {
            "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "swarm_id": {"type": "string", "minLength": 1},
        },
        "required": ["swarm_id"],
        "additionalProperties": False,
    },
    "swarms.settings": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "delivery": {"type": "object"},
            "expected_revision": {"type": "integer", "minimum": 1},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["swarm_id", "delivery", "expected_revision", "request_id"],
        "additionalProperties": False,
    },
    "swarms.start": {
        "type": "object",
        "properties": {
            "profile_id": {"type": "string", "minLength": 1},
            "prompt": {"type": "string", "minLength": 1, "maxLength": 16000},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "expected_profile_revision": {"type": "integer", "minimum": 1},
            "working_directory": {"type": "string", "minLength": 1},
        },
        "required": ["profile_id", "prompt", "request_id"],
        "additionalProperties": False,
    },
    "swarms.stop": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["swarm_id", "request_id"],
        "additionalProperties": False,
    },
    "swarms.resume": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "participant_id": {"type": "string", "minLength": 1},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["swarm_id", "request_id"],
        "additionalProperties": False,
    },
    "swarms.usage": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "participant_id": {"type": "string", "minLength": 1},
        },
        "required": ["swarm_id"],
        "additionalProperties": False,
    },
    "board.list": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["swarm_id"],
        "additionalProperties": False,
    },
    "board.read": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "discussion_id": {"type": "string", "minLength": 1},
            "message_id": {"type": "string", "minLength": 1},
            "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["swarm_id"],
        "additionalProperties": False,
    },
    "board.post": {
        "type": "object",
        "properties": {
            "swarm_id": {"type": "string", "minLength": 1},
            "discussion_id": {"type": "string", "minLength": 1},
            "text": {"type": "string", "minLength": 1, "maxLength": 16000},
            "reply_to": {"type": "string", "minLength": 1},
            "recipients": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["swarm_id", "text", "request_id"],
        "additionalProperties": False,
    },
}
