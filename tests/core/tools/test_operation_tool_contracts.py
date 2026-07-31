"""Cross-Tool tests for provider-visible operation contracts."""

from __future__ import annotations

from typing import Any

import pytest

from core.providers.tool_schema import render_tool_definitions, render_tool_schema
from core.tools.bash import BASH_TOOL_PARAMETERS
from core.tools.channel import CHANNEL_SEND_TOOL_PARAMETERS
from core.tools.contracts import ToolContractError, compile_tool_contract
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
from core.tools.session_search import (
    SESSION_READ_TOOL_PARAMETERS,
    SESSION_SEARCH_TOOL_PARAMETERS,
)
from core.tools.skill import SKILL_TOOL_PARAMETERS
from core.tools.skill_manage import SKILL_MANAGE_TOOL_PARAMETERS
from core.tools.speech import TEXT_TO_SPEECH_TOOL_PARAMETERS
from core.tools.status import STATUS_TOOL_PARAMETERS
from core.tools.subagent import SUBAGENT_TOOL_PARAMETERS
from core.tools.web_fetch import WEB_FETCH_TOOL_PARAMETERS
from core.tools.web_search import WEB_SEARCH_TOOL_PARAMETERS
from core.tools.write import WRITE_TOOL_PARAMETERS

JsonObject = dict[str, Any]

_DIRECT_TOOL_SCHEMAS: tuple[tuple[str, JsonObject], ...] = (
    ("analyze_image", ANALYZE_IMAGE_TOOL_PARAMETERS),
    ("bash", BASH_TOOL_PARAMETERS),
    ("channel_send", CHANNEL_SEND_TOOL_PARAMETERS),
    ("cron", CRON_TOOL_PARAMETERS),
    ("edit", EDIT_TOOL_PARAMETERS),
    ("glob", GLOB_TOOL_PARAMETERS),
    ("grep", GREP_TOOL_PARAMETERS),
    ("history", HISTORY_TOOL_PARAMETERS),
    ("image_generation", IMAGE_GENERATION_TOOL_PARAMETERS),
    ("memory", MEMORY_TOOL_PARAMETERS),
    ("process", PROCESS_TOOL_PARAMETERS),
    ("project", PROJECT_TOOL_PARAMETERS),
    ("read", READ_TOOL_PARAMETERS),
    ("session_read", SESSION_READ_TOOL_PARAMETERS),
    ("session_search", SESSION_SEARCH_TOOL_PARAMETERS),
    ("skill", SKILL_TOOL_PARAMETERS),
    ("skill_manage", SKILL_MANAGE_TOOL_PARAMETERS),
    ("status", STATUS_TOOL_PARAMETERS),
    ("subagent", SUBAGENT_TOOL_PARAMETERS),
    ("text_to_speech", TEXT_TO_SPEECH_TOOL_PARAMETERS),
    ("web_fetch", WEB_FETCH_TOOL_PARAMETERS),
    ("web_search", WEB_SEARCH_TOOL_PARAMETERS),
    ("write", WRITE_TOOL_PARAMETERS),
)

_OPENAI_STRICT_SHIPPED_TOOLS = {
    "analyze_image",
    "project",
    "skill",
    "text_to_speech",
    "write",
}

_BRANCH_COMPLETE_SCHEMAS = (
    ("bash", BASH_TOOL_PARAMETERS),
    ("cron", CRON_TOOL_PARAMETERS),
    ("grep", GREP_TOOL_PARAMETERS),
    ("history", HISTORY_TOOL_PARAMETERS),
    ("memory", MEMORY_TOOL_PARAMETERS),
    ("process", PROCESS_TOOL_PARAMETERS),
    ("skill_manage", SKILL_MANAGE_TOOL_PARAMETERS),
    ("status", STATUS_TOOL_PARAMETERS),
    ("subagent", SUBAGENT_TOOL_PARAMETERS),
)

_BRANCH_INAPPLICABLE_CALLS = (
    (
        "bash",
        BASH_TOOL_PARAMETERS,
        {"mode": "foreground", "command": "echo ok", "yield_after": 30},
        "yield_after",
    ),
    ("cron", CRON_TOOL_PARAMETERS, {"action": "list", "id": "job"}, "id"),
    (
        "grep",
        GREP_TOOL_PARAMETERS,
        {"pattern": "text", "output_mode": "count", "context": 2},
        "context",
    ),
    ("history", HISTORY_TOOL_PARAMETERS, {"action": "overview", "query": "text"}, "query"),
    (
        "memory",
        MEMORY_TOOL_PARAMETERS,
        {"action": "add", "scope": "user", "content": "fact", "entry_id": 1},
        "entry_id",
    ),
    ("process", PROCESS_TOOL_PARAMETERS, {"action": "status", "text": "hello"}, "text"),
    (
        "skill_manage",
        SKILL_MANAGE_TOOL_PARAMETERS,
        {"action": "delete", "name": "demo", "content": "text"},
        "content",
    ),
    ("status", STATUS_TOOL_PARAMETERS, {"agent_id": "coder"}, "agent_id"),
    (
        "subagent",
        SUBAGENT_TOOL_PARAMETERS,
        {"action": "status", "content": "work"},
        "content",
    ),
)

_BRANCH_MISSING_REQUIRED_CALLS: tuple[tuple[str, JsonObject, JsonObject, str], ...] = (
    ("bash", BASH_TOOL_PARAMETERS, {"mode": "auto"}, "command"),
    ("cron", CRON_TOOL_PARAMETERS, {"action": "update", "id": "job"}, "minProperties"),
    ("history", HISTORY_TOOL_PARAMETERS, {"action": "search"}, "query"),
    (
        "memory",
        MEMORY_TOOL_PARAMETERS,
        {"action": "add", "scope": "user"},
        "content",
    ),
    ("process", PROCESS_TOOL_PARAMETERS, {"action": "input", "session_id": "proc"}, "text"),
    ("skill", SKILL_TOOL_PARAMETERS, {}, "name"),
    (
        "skill_manage",
        SKILL_MANAGE_TOOL_PARAMETERS,
        {"action": "create", "name": "demo"},
        "content",
    ),
    (
        "subagent",
        SUBAGENT_TOOL_PARAMETERS,
        {"action": "run", "content": "continue", "session_id": "session"},
        "agent_id",
    ),
)


def test_shipped_tool_profile_eligibility_snapshot_is_explicit() -> None:
    schemas = list(_DIRECT_TOOL_SCHEMAS)
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
    "profile",
    ("openai_strict", "anthropic_strict", "best_effort"),
)
def test_skill_schema_exposes_required_name_in_every_provider_profile(profile: str) -> None:
    rendered = render_tool_definitions(
        [
            {
                "name": "skill",
                "description": "Activate a Skill.",
                "parameters": SKILL_TOOL_PARAMETERS,
            }
        ],
        profile=profile,  # type: ignore[arg-type]
    )[0]
    parameters = rendered["parameters"]

    assert set(parameters["properties"]) == {"name", "file_path"}
    assert "name" in parameters["required"]
    if profile == "openai_strict":
        assert rendered["strict"] is True
        assert set(parameters["required"]) == {"name", "file_path"}
    elif profile == "anthropic_strict":
        assert rendered["strict"] is True
        assert parameters == SKILL_TOOL_PARAMETERS
    else:
        assert "strict" not in rendered
        assert parameters == SKILL_TOOL_PARAMETERS


@pytest.mark.parametrize(
    ("tool_name", "schema"),
    _BRANCH_COMPLETE_SCHEMAS,
    ids=[contract[0] for contract in _BRANCH_COMPLETE_SCHEMAS],
)
@pytest.mark.parametrize("profile", ("openai_strict", "anthropic_strict", "best_effort"))
def test_branch_complete_schema_reaches_provider_unchanged(
    tool_name: str,
    schema: JsonObject,
    profile: str,
) -> None:
    rendered = render_tool_definitions(
        [{"name": tool_name, "description": f"Call {tool_name}.", "parameters": schema}],
        profile=profile,  # type: ignore[arg-type]
    )

    assert rendered[0]["parameters"] == schema
    assert "strict" not in rendered[0]


@pytest.mark.parametrize(
    ("tool_name", "schema", "arguments", "field_name"),
    _BRANCH_INAPPLICABLE_CALLS,
    ids=[contract[0] for contract in _BRANCH_INAPPLICABLE_CALLS],
)
def test_branch_complete_schema_rejects_inapplicable_fields(
    tool_name: str,
    schema: JsonObject,
    arguments: JsonObject,
    field_name: str,
) -> None:
    contract = compile_tool_contract(name=tool_name, input_schema=schema)

    with pytest.raises(ToolContractError, match=field_name):
        contract.validate_arguments(arguments)


@pytest.mark.parametrize(
    ("tool_name", "schema", "arguments", "requirement"),
    _BRANCH_MISSING_REQUIRED_CALLS,
    ids=[contract[0] for contract in _BRANCH_MISSING_REQUIRED_CALLS],
)
def test_branch_complete_schema_enforces_variant_requirements(
    tool_name: str,
    schema: JsonObject,
    arguments: JsonObject,
    requirement: str,
) -> None:
    contract = compile_tool_contract(name=tool_name, input_schema=schema)

    with pytest.raises(ToolContractError, match=requirement):
        contract.validate_arguments(arguments)


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
    variants = schema.get("oneOf", [schema]) if "properties" not in schema else [schema]
    assert isinstance(variants, list) and variants, tool_name
    for variant in variants:
        assert variant["type"] == "object", tool_name
        assert variant["additionalProperties"] is False, tool_name
        assert set(variant.get("required", ())) <= set(variant["properties"]), tool_name
