"""Cross-Tool tests for provider-visible operation contracts."""

from __future__ import annotations

from typing import Any

import pytest

from core.providers.tool_schema import sanitize_anthropic_tool_input_schema
from core.tools.bash import BASH_TOOL_PARAMETERS
from core.tools.channel import CHANNEL_SEND_TOOL_PARAMETERS
from core.tools.cron import CRON_TOOL_PARAMETERS
from core.tools.edit import EDIT_TOOL_PARAMETERS
from core.tools.glob import GLOB_TOOL_PARAMETERS
from core.tools.grep import GREP_TOOL_PARAMETERS
from core.tools.history import HISTORY_TOOL_PARAMETERS
from core.tools.image import (
    ANALYZE_IMAGE_TOOL_PARAMETERS,
    IMAGE_GENERATION_TOOL_PARAMETERS,
)
from core.tools.memory import MEMORY_TOOL_PARAMETERS
from core.tools.process import PROCESS_TOOL_PARAMETERS
from core.tools.project import PROJECT_TOOL_PARAMETERS
from core.tools.read import READ_TOOL_PARAMETERS
from core.tools.session_search import SESSION_SEARCH_TOOL_PARAMETERS
from core.tools.skill import SKILL_TOOL_PARAMETERS
from core.tools.skill_manage import SKILL_MANAGE_TOOL_PARAMETERS
from core.tools.speech import TEXT_TO_SPEECH_TOOL_PARAMETERS
from core.tools.status import STATUS_TOOL_PARAMETERS
from core.tools.subagent import SUBAGENT_RESULT_TOOL_PARAMETERS, SUBAGENT_TOOL_PARAMETERS
from core.tools.tools import extract_tool_operation, operation_envelope_schema
from core.tools.web_fetch import WEB_FETCH_TOOL_PARAMETERS
from core.tools.web_search import WEB_SEARCH_TOOL_PARAMETERS
from core.tools.write import WRITE_TOOL_PARAMETERS

JsonObject = dict[str, Any]

_OPERATION_CONTRACTS: tuple[
    tuple[str, JsonObject, dict[str, tuple[str, ...]]],
    ...,
] = (
    (
        "channel_send",
        CHANNEL_SEND_TOOL_PARAMETERS,
        {"send": ("channel_id",)},
    ),
    (
        "cron",
        CRON_TOOL_PARAMETERS,
        {
            "create": ("name", "prompt", "schedule_type"),
            "list": (),
            "update": ("id",),
            "delete": ("id",),
            "enable": ("id",),
            "disable": ("id",),
        },
    ),
    (
        "history",
        HISTORY_TOOL_PARAMETERS,
        {
            "overview": (),
            "search": ("query",),
            "read": (),
            "around": ("message_id",),
        },
    ),
    (
        "memory",
        MEMORY_TOOL_PARAMETERS,
        {
            "list": ("scope",),
            "add": ("scope", "content"),
            "replace": ("scope", "entry_id", "content"),
            "remove": ("scope", "entry_id"),
        },
    ),
    (
        "process",
        PROCESS_TOOL_PARAMETERS,
        {
            "list": (),
            "poll": ("session_id",),
            "log": ("session_id",),
            "write": ("session_id", "data"),
            "submit": ("session_id",),
            "kill": ("session_id",),
            "clear": ("session_id",),
        },
    ),
    (
        "session_search",
        SESSION_SEARCH_TOOL_PARAMETERS,
        {
            "list": (),
            "overview": ("session_id",),
            "search": ("query",),
            "read": ("session_id",),
        },
    ),
    (
        "skill_manage",
        SKILL_MANAGE_TOOL_PARAMETERS,
        {
            "inspect": (),
            "begin": ("name", "mode"),
            "put_file": ("draft_id", "path"),
            "patch": ("draft_id", "old_string", "new_string"),
            "remove_file": ("draft_id", "path"),
            "validate": ("draft_id",),
            "commit": ("draft_id",),
            "abort": ("draft_id",),
            "delete": ("name",),
        },
    ),
    (
        "status",
        STATUS_TOOL_PARAMETERS,
        {
            "current": (),
            "session": ("session_id",),
            "agent_session": ("agent_id", "session_id"),
        },
    ),
    (
        "subagent",
        SUBAGENT_TOOL_PARAMETERS,
        {
            "start": ("content",),
            "continue": ("content", "agent_id", "session_id"),
        },
    ),
)

_DIRECT_TOOL_SCHEMAS: tuple[tuple[str, JsonObject], ...] = (
    ("analyze_image", ANALYZE_IMAGE_TOOL_PARAMETERS),
    ("bash", BASH_TOOL_PARAMETERS),
    ("edit", EDIT_TOOL_PARAMETERS),
    ("glob", GLOB_TOOL_PARAMETERS),
    ("grep", GREP_TOOL_PARAMETERS),
    ("image_generation", IMAGE_GENERATION_TOOL_PARAMETERS),
    ("project", PROJECT_TOOL_PARAMETERS),
    ("read", READ_TOOL_PARAMETERS),
    ("skill", SKILL_TOOL_PARAMETERS),
    ("subagent_result", SUBAGENT_RESULT_TOOL_PARAMETERS),
    ("text_to_speech", TEXT_TO_SPEECH_TOOL_PARAMETERS),
    ("web_fetch", WEB_FETCH_TOOL_PARAMETERS),
    ("web_search", WEB_SEARCH_TOOL_PARAMETERS),
    ("write", WRITE_TOOL_PARAMETERS),
)


def _normal_operation_schema(operation_schema: JsonObject) -> JsonObject:
    if "properties" in operation_schema:
        return operation_schema
    branches = operation_schema.get("oneOf")
    assert isinstance(branches, list) and branches
    normal = branches[0]
    assert isinstance(normal, dict)
    return normal


@pytest.mark.parametrize(
    ("tool_name", "schema", "required_by_operation"),
    _OPERATION_CONTRACTS,
    ids=[contract[0] for contract in _OPERATION_CONTRACTS],
)
def test_multi_operation_tool_schema_is_exact_and_action_complete(
    tool_name: str,
    schema: JsonObject,
    required_by_operation: dict[str, tuple[str, ...]],
) -> None:
    assert schema["type"] == "object", tool_name
    assert schema["minProperties"] == 1, tool_name
    assert schema["maxProperties"] == 1, tool_name
    assert schema["additionalProperties"] is False, tool_name
    assert set(schema["properties"]) == set(required_by_operation), tool_name

    for operation, expected_required in required_by_operation.items():
        operation_schema = schema["properties"][operation]
        assert operation_schema["type"] == "object", f"{tool_name}.{operation}"
        normal = _normal_operation_schema(operation_schema)
        assert normal["additionalProperties"] is False, f"{tool_name}.{operation}"
        assert tuple(normal.get("required", ())) == expected_required, f"{tool_name}.{operation}"
        assert set(expected_required) <= set(normal["properties"]), f"{tool_name}.{operation}"


@pytest.mark.parametrize(
    ("tool_name", "schema", "_required_by_operation"),
    _OPERATION_CONTRACTS,
    ids=[contract[0] for contract in _OPERATION_CONTRACTS],
)
def test_operation_contract_survives_anthropic_schema_sanitization(
    tool_name: str,
    schema: JsonObject,
    _required_by_operation: dict[str, tuple[str, ...]],
) -> None:
    assert sanitize_anthropic_tool_input_schema(schema, tool_name=tool_name) == schema


@pytest.mark.parametrize(
    ("tool_name", "schema"),
    _DIRECT_TOOL_SCHEMAS,
    ids=[contract[0] for contract in _DIRECT_TOOL_SCHEMAS],
)
def test_direct_tool_schema_is_strict_and_declares_required_properties(
    tool_name: str,
    schema: JsonObject,
) -> None:
    assert schema["type"] == "object", tool_name
    assert schema["additionalProperties"] is False, tool_name
    assert set(schema.get("required", ())) <= set(schema["properties"]), tool_name


def test_operation_envelope_builder_rejects_invalid_definitions() -> None:
    with pytest.raises(ValueError, match="at least one"):
        operation_envelope_schema({}, description="empty")
    with pytest.raises(ValueError, match="must be an object"):
        operation_envelope_schema(
            {"run": {"type": "string"}},
            description="invalid",
        )


def test_operation_extractor_accepts_canonical_and_legacy_calls() -> None:
    assert extract_tool_operation({"list": {}}, ("list", "read")) == ("list", {})
    assert extract_tool_operation(
        {"action": "read", "message_id": "m1"},
        ("list", "read"),
    ) == ("read", {"message_id": "m1"})


@pytest.mark.parametrize(
    "arguments",
    (
        {},
        {"list": {}, "read": {"message_id": "m1"}},
        {"unknown": {}},
        {"list": "not-an-object"},
    ),
)
def test_operation_extractor_rejects_ambiguous_or_invalid_calls(
    arguments: JsonObject,
) -> None:
    with pytest.raises(ValueError):
        extract_tool_operation(arguments, ("list", "read"))
