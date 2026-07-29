"""Cross-Tool tests for provider-visible operation contracts."""

from __future__ import annotations

from typing import Any

import pytest

from core.providers.tool_schema import render_tool_definitions, render_tool_schema
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
    "text_to_speech",
    "write",
}


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
