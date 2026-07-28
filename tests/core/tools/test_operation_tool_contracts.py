"""Cross-Tool tests for provider-visible operation contracts."""

from __future__ import annotations

from typing import Any

import pytest

from core.providers.tool_schema import (
    render_tool_definitions,
    render_tool_schema,
    sanitize_anthropic_tool_input_schema,
)
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
        "subagent",
        SUBAGENT_TOOL_PARAMETERS,
        {
            "start": ("content",),
            "continue": ("content", "agent_id", "session_id"),
            "cancel": ("agent_id", "session_id"),
        },
    ),
)

_DIRECT_TOOL_SCHEMAS: tuple[tuple[str, JsonObject], ...] = (
    ("analyze_image", ANALYZE_IMAGE_TOOL_PARAMETERS),
    ("bash", BASH_TOOL_PARAMETERS),
    ("cron", CRON_TOOL_PARAMETERS),
    ("edit", EDIT_TOOL_PARAMETERS),
    ("glob", GLOB_TOOL_PARAMETERS),
    ("grep", GREP_TOOL_PARAMETERS),
    ("history", HISTORY_TOOL_PARAMETERS),
    ("image_generation", IMAGE_GENERATION_TOOL_PARAMETERS),
    ("memory", MEMORY_TOOL_PARAMETERS),
    ("project", PROJECT_TOOL_PARAMETERS),
    ("read", READ_TOOL_PARAMETERS),
    ("skill", SKILL_TOOL_PARAMETERS),
    ("skill_manage", SKILL_MANAGE_TOOL_PARAMETERS),
    ("status", STATUS_TOOL_PARAMETERS),
    ("subagent_result", SUBAGENT_RESULT_TOOL_PARAMETERS),
    ("text_to_speech", TEXT_TO_SPEECH_TOOL_PARAMETERS),
    ("web_fetch", WEB_FETCH_TOOL_PARAMETERS),
    ("web_search", WEB_SEARCH_TOOL_PARAMETERS),
    ("write", WRITE_TOOL_PARAMETERS),
)

_OPENAI_STRICT_SHIPPED_TOOLS = {
    "analyze_image",
    "project",
    "text_to_speech",
    "write",
}


def test_shipped_tool_profile_eligibility_snapshot_is_explicit() -> None:
    schemas = [
        *((name, schema) for name, schema, _required in _OPERATION_CONTRACTS),
        *_DIRECT_TOOL_SCHEMAS,
    ]
    openai_decisions = {
        name: render_tool_schema(schema, profile="openai_strict") for name, schema in schemas
    }
    anthropic_definitions = render_tool_definitions(
        [
            {"name": name, "description": f"Call {name}.", "parameters": schema}
            for name, schema in schemas
        ],
        profile="anthropic_strict",
    )

    assert {
        name for name, decision in openai_decisions.items() if decision.strict
    } == _OPENAI_STRICT_SHIPPED_TOOLS
    assert all(
        decision.strict or decision.reason is not None for decision in openai_decisions.values()
    )
    assert all("strict" not in definition for definition in anthropic_definitions)


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
    assert schema["additionalProperties"] is False, tool_name
    assert schema["required"] == ["request"], tool_name
    assert set(schema["properties"]) == {"request"}, tool_name
    request = schema["properties"]["request"]
    assert request["type"] == "object", tool_name
    branches = request["anyOf"]

    for operation, expected_required in required_by_operation.items():
        operation_branches = [
            branch
            for branch in branches
            if branch["properties"]["operation"]["enum"] == [operation]
        ]
        assert operation_branches, f"{tool_name}.{operation}"
        assert any(
            set(expected_required)
            <= {field for field in branch["required"] if field != "operation"}
            for branch in operation_branches
        ), f"{tool_name}.{operation}"
        for branch in operation_branches:
            assert branch["type"] == "object", f"{tool_name}.{operation}"
            assert branch["additionalProperties"] is False, f"{tool_name}.{operation}"
            assert branch["required"][0] == "operation", f"{tool_name}.{operation}"
            assert set(branch["required"]) <= set(branch["properties"]), f"{tool_name}.{operation}"


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


def test_operation_extractor_accepts_only_canonical_calls() -> None:
    assert extract_tool_operation(
        {"request": {"operation": "read", "message_id": "m1"}},
        ("list", "read"),
    ) == ("read", {"message_id": "m1"})


@pytest.mark.parametrize(
    "arguments",
    (
        {},
        {"request": {"operation": "list"}, "read": {"message_id": "m1"}},
        {"unknown": {}},
        {"request": "not-an-object"},
        {"action": "read", "message_id": "m1"},
        {"list": {}},
    ),
)
def test_operation_extractor_rejects_ambiguous_or_invalid_calls(
    arguments: JsonObject,
) -> None:
    with pytest.raises(ValueError):
        extract_tool_operation(arguments, ("list", "read"))
