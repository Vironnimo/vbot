"""Cross-Tool tests for provider-visible operation contracts."""

from __future__ import annotations

from typing import Any

import pytest

from core.providers.tool_schema import render_tool_definitions
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
from core.tools.session_search import (
    SESSION_READ_TOOL_PARAMETERS,
    SESSION_SEARCH_TOOL_PARAMETERS,
)
from core.tools.skill import SKILL_LIST_TOOL_PARAMETERS, SKILL_TOOL_PARAMETERS
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


@pytest.mark.parametrize(
    "profile",
    ("explicit_non_strict", "omit_strict"),
)
def test_skill_schema_exposes_direct_fields_in_every_provider_profile(profile: str) -> None:
    rendered = render_tool_definitions(
        [
            {
                "name": "skill",
                "description": "Activate or read a Skill.",
                "parameters": SKILL_TOOL_PARAMETERS,
            }
        ],
        profile=profile,  # type: ignore[arg-type]
    )[0]

    assert set(rendered["parameters"]["properties"]) == {"name", "file_path"}
    assert rendered["parameters"]["required"] == ["name"]
    assert rendered["parameters"] == SKILL_TOOL_PARAMETERS
    if profile == "explicit_non_strict":
        assert rendered["strict"] is False
    else:
        assert "strict" not in rendered


@pytest.mark.parametrize(
    "profile",
    ("explicit_non_strict", "omit_strict"),
)
def test_skill_list_schema_is_an_empty_call_in_every_provider_profile(profile: str) -> None:
    rendered = render_tool_definitions(
        [
            {
                "name": "skill_list",
                "description": "List Skills during Reflection.",
                "parameters": SKILL_LIST_TOOL_PARAMETERS,
            }
        ],
        profile=profile,  # type: ignore[arg-type]
    )[0]

    assert rendered["parameters"] == SKILL_LIST_TOOL_PARAMETERS
    if profile == "explicit_non_strict":
        assert rendered["strict"] is False
    else:
        assert "strict" not in rendered


@pytest.mark.parametrize(
    ("tool_name", "schema"),
    _DIRECT_TOOL_SCHEMAS,
    ids=[contract[0] for contract in _DIRECT_TOOL_SCHEMAS],
)
def test_direct_tool_schema_is_flat_and_declares_required_properties(
    tool_name: str,
    schema: JsonObject,
) -> None:
    assert schema["type"] == "object", tool_name
    if tool_name in {
        "analyze_image",
        "bash",
        "channel_send",
        "cron",
        "edit",
        "glob",
        "grep",
        "history",
        "image_generation",
        "memory",
        "process",
        "project",
        "read",
        "session_read",
        "session_search",
        "skill",
        "skill_manage",
        "status",
        "subagent",
        "text_to_speech",
        "web_fetch",
        "web_search",
        "write",
    }:
        assert "oneOf" not in schema
        assert "additionalProperties" not in schema
        assert set(schema.get("required", ())) <= set(schema["properties"]), tool_name
        return
    variants = schema.get("oneOf", [schema]) if "properties" not in schema else [schema]
    assert isinstance(variants, list) and variants, tool_name
    for variant in variants:
        assert variant["type"] == "object", tool_name
        assert variant["additionalProperties"] is False, tool_name
        assert set(variant.get("required", ())) <= set(variant["properties"]), tool_name
