"""Provider Tool probe: scenarios."""

from __future__ import annotations

import argparse
import json
from typing import Any

from scripts.provider_probe.common import (
    PROBE_TOOL,
    PROBE_TOOL_NAME,
    ProbeScenario,
    _probe_content,
    _probe_messages,
)
from scripts.provider_probe.computer_cases import COMPUTER_CASE_ARGUMENTS
from scripts.provider_probe.scenario_agents import (
    _bash_scenario,
    _channel_send_scenario,
    _process_scenario,
    _project_scenario,
    _skill_manage_scenario,
    _skill_scenario,
    _subagent_scenario,
)
from scripts.provider_probe.scenario_automation import _calendar_scenario, _cron_scenario
from scripts.provider_probe.scenario_extensions import (
    _ha_call_service_scenario,
    _ha_get_state_scenario,
    _ha_list_entities_scenario,
    _ha_list_services_scenario,
    _mcp_scenario,
    _word_count_scenario,
)
from scripts.provider_probe.scenario_files import (
    _edit_scenario,
    _glob_scenario,
    _grep_scenario,
    _read_scenario,
    _write_scenario,
)
from scripts.provider_probe.scenario_history import (
    _history_scenario,
    _memory_scenario,
    _session_read_scenario,
    _session_search_scenario,
    _status_scenario,
)
from scripts.provider_probe.scenario_media import (
    _analyze_image_scenario,
    _image_generation_scenario,
    _text_to_speech_scenario,
)
from scripts.provider_probe.scenario_web import _web_fetch_scenario, _web_search_scenario


def _optional_boolean_scenario(
    name: str,
    *,
    schema_defaults: bool,
    describe_defaults: bool,
    case_name: str,
) -> ProbeScenario:
    include_links: dict[str, Any] = {"type": "boolean"}
    raw: dict[str, Any] = {"type": "boolean"}
    if describe_defaults:
        include_links["description"] = (
            "Optional JSON boolean. Omit it to preserve Markdown links; the value used "
            "when omitted is true. Set false only to remove link targets."
        )
        raw["description"] = (
            "Optional JSON boolean. Omit it for cleaned text; the value used when "
            "omitted is false. Set true only to request raw HTML."
        )
    else:
        include_links["description"] = (
            "Optional JSON boolean. Send it only when the user explicitly requests a "
            "value; otherwise omit the field."
        )
        raw["description"] = (
            "Optional JSON boolean. Send it only when the user explicitly requests a "
            "value; otherwise omit the field."
        )
    if schema_defaults:
        include_links["default"] = True
        raw["default"] = False
    tool = {
        "name": PROBE_TOOL_NAME,
        "description": "Inspect one synthetic URL without fetching or changing external state.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1},
                "include_links": include_links,
                "raw": raw,
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    }
    instructions = {
        "omit": (
            "Call the supplied Tool exactly once with url=https://example.com/omit. "
            "Omit both include_links and raw. Do not add either omitted field."
        ),
        "include_links": (
            "Call the supplied Tool exactly once with url=https://example.com/links and "
            "include_links=false. Omit raw. Do not add the omitted field."
        ),
        "raw": (
            "Call the supplied Tool exactly once with url=https://example.com/raw and "
            "raw=true. Omit include_links. Do not add the omitted field."
        ),
        "both": (
            "Call the supplied Tool exactly once with url=https://example.com/both, "
            "include_links=false, and raw=true."
        ),
    }
    return ProbeScenario(
        name,
        [tool],
        _probe_messages(instructions[case_name]),
        PROBE_TOOL_NAME,
    )


def _scenario(args: argparse.Namespace) -> ProbeScenario:
    direct = json.loads(json.dumps(PROBE_TOOL))
    name = str(args.scenario)
    if name == "computer":
        from resources.extensions.computer_use.extension import (
            COMPUTER_DESCRIPTION,
            COMPUTER_PARAMETERS,
        )

        expected = COMPUTER_CASE_ARGUMENTS[args.computer_case]
        instruction = (
            "This is an inert Tool-contract test; no desktop action will execute. "
            "The target is a test-owned window with a fresh capture and element 1 / s00000001:1. "
            "The user explicitly requested foreground input where specified. "
            "Emit exactly one computer call using only these arguments, "
            "including deliberate invalid cases: " + json.dumps(expected)
        )
        return ProbeScenario(
            name,
            [
                {
                    "name": "computer",
                    "description": COMPUTER_DESCRIPTION,
                    "parameters": COMPUTER_PARAMETERS,
                }
            ],
            _probe_messages(instruction),
            "computer",
            require_closed_input=False,
            expected_arguments=expected,
        )
    if name == "direct_required":
        return ProbeScenario(name, [direct], _probe_messages("Inspect key alpha."), PROBE_TOOL_NAME)
    if name == "nested_operation":
        nested = {
            "name": PROBE_TOOL_NAME,
            "description": "Inspect one synthetic key or list synthetic keys.",
            "parameters": {
                "type": "object",
                "properties": {
                    "request": {
                        "type": "object",
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "operation": {"type": "string", "enum": ["inspect"]},
                                    "key": {"type": "string", "minLength": 1},
                                },
                                "required": ["operation", "key"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "operation": {"type": "string", "enum": ["list"]},
                                },
                                "required": ["operation"],
                                "additionalProperties": False,
                            },
                        ],
                    }
                },
                "required": ["request"],
                "additionalProperties": False,
            },
        }
        return ProbeScenario(
            name,
            [nested],
            _probe_messages("Use the inspect operation for key alpha."),
            PROBE_TOOL_NAME,
        )
    if name == "optional_null":
        direct["parameters"]["properties"]["note"] = {
            "type": ["string", "null"],
            "description": "Optional synthetic note; null and omission have the same meaning.",
        }
        return ProbeScenario(
            name,
            [direct],
            _probe_messages("Inspect key alpha without a note."),
            PROBE_TOOL_NAME,
        )
    if name in {
        "optional_booleans",
        "optional_booleans_bare",
        "optional_booleans_schema_defaults",
    }:
        return _optional_boolean_scenario(
            name,
            schema_defaults=name == "optional_booleans_schema_defaults",
            describe_defaults=name != "optional_booleans_bare",
            case_name=str(getattr(args, "optional_case", "omit")),
        )
    if name == "wrong_type_pressure":
        direct["parameters"]["properties"]["count"] = {"type": "integer", "minimum": 1}
        direct["parameters"]["required"].append("count")
        return ProbeScenario(
            name,
            [direct],
            _probe_messages('Inspect key alpha with count shown as quoted text "7".'),
            PROBE_TOOL_NAME,
        )
    if name == "missing_required_pressure":
        return ProbeScenario(
            name,
            [direct],
            _probe_messages("Call the inspection Tool but omit its required key."),
            PROBE_TOOL_NAME,
        )
    if name == "unknown_property_pressure":
        return ProbeScenario(
            name,
            [direct],
            _probe_messages("Inspect key alpha and also include an extra field named surprise."),
            PROBE_TOOL_NAME,
        )
    if name == "large_arguments":
        content = _probe_content(args.lines)
        direct["parameters"]["properties"]["content"] = {"type": "string", "minLength": 1}
        direct["parameters"]["required"].append("content")
        return ProbeScenario(
            name,
            [direct],
            _probe_messages(
                "Inspect key alpha and copy the payload between the markers verbatim into "
                f"content.\n<PAYLOAD>\n{content}\n</PAYLOAD>"
            ),
            PROBE_TOOL_NAME,
        )
    if name == "analyze_image":
        return _analyze_image_scenario(str(args.analyze_image_case))
    if name == "bash":
        return _bash_scenario(str(args.bash_case))
    if name == "calendar":
        return _calendar_scenario(str(args.calendar_case))
    if name == "channel_send":
        return _channel_send_scenario(str(args.channel_send_case))
    if name == "cron":
        return _cron_scenario(str(args.cron_case))
    if name == "edit":
        return _edit_scenario(str(args.edit_case))
    if name == "glob":
        return _glob_scenario(str(args.glob_case))
    if name == "grep":
        return _grep_scenario(str(args.grep_case))
    if name == "ha_call_service":
        return _ha_call_service_scenario(str(args.ha_call_service_case))
    if name == "ha_get_state":
        return _ha_get_state_scenario(str(args.ha_get_state_case))
    if name == "ha_list_entities":
        return _ha_list_entities_scenario(str(args.ha_list_entities_case))
    if name == "ha_list_services":
        return _ha_list_services_scenario(str(args.ha_list_services_case))
    if name == "history":
        return _history_scenario(str(args.history_case))
    if name == "image_generation":
        return _image_generation_scenario(str(args.image_generation_case))
    if name == "mcp":
        return _mcp_scenario(str(args.mcp_case))
    if name == "memory":
        return _memory_scenario(str(args.memory_case))
    if name == "process":
        return _process_scenario(str(args.process_case))
    if name == "project":
        return _project_scenario()
    if name == "read":
        return _read_scenario(str(args.read_case))
    if name == "session_read":
        return _session_read_scenario(str(args.session_read_case))
    if name == "session_search":
        return _session_search_scenario(str(args.session_search_case))
    if name == "skill":
        return _skill_scenario(str(args.skill_case))
    if name == "skill_manage":
        return _skill_manage_scenario(str(args.skill_manage_case))
    if name == "status":
        return _status_scenario(str(args.status_case))
    if name == "subagent":
        return _subagent_scenario(str(args.subagent_case))
    if name == "text_to_speech":
        return _text_to_speech_scenario(str(args.speech_case))
    if name == "web_fetch":
        return _web_fetch_scenario(str(args.web_fetch_case))
    if name == "web_search":
        return _web_search_scenario(str(args.web_search_case))
    if name == "write":
        return _write_scenario()
    if name == "word_count":
        return _word_count_scenario(str(args.word_count_case))
    raise AssertionError(f"unsupported probe scenario: {name}")
