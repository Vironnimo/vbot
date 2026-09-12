"""Provider Tool probe: scenario extensions."""

from __future__ import annotations

import importlib
import json
from typing import Any

from scripts.provider_probe.choices import MCP_CASE_ARGUMENTS
from scripts.provider_probe.common import ProbeScenario, _probe_messages

_HOMEASSISTANT_EXTENSION = importlib.import_module("resources.extensions.homeassistant.extension")


HA_LIST_ENTITIES_DESCRIPTION = _HOMEASSISTANT_EXTENSION.HA_LIST_ENTITIES_DESCRIPTION


HA_LIST_ENTITIES_NAME = _HOMEASSISTANT_EXTENSION.HA_LIST_ENTITIES_NAME


HA_LIST_ENTITIES_PARAMETERS = _HOMEASSISTANT_EXTENSION.HA_LIST_ENTITIES_PARAMETERS


HA_GET_STATE_DESCRIPTION = _HOMEASSISTANT_EXTENSION.HA_GET_STATE_DESCRIPTION


HA_GET_STATE_NAME = _HOMEASSISTANT_EXTENSION.HA_GET_STATE_NAME


HA_GET_STATE_PARAMETERS = _HOMEASSISTANT_EXTENSION.HA_GET_STATE_PARAMETERS


HA_LIST_SERVICES_DESCRIPTION = _HOMEASSISTANT_EXTENSION.HA_LIST_SERVICES_DESCRIPTION


HA_LIST_SERVICES_NAME = _HOMEASSISTANT_EXTENSION.HA_LIST_SERVICES_NAME


HA_LIST_SERVICES_PARAMETERS = _HOMEASSISTANT_EXTENSION.HA_LIST_SERVICES_PARAMETERS


HA_CALL_SERVICE_DESCRIPTION = _HOMEASSISTANT_EXTENSION.HA_CALL_SERVICE_DESCRIPTION


HA_CALL_SERVICE_NAME = _HOMEASSISTANT_EXTENSION.HA_CALL_SERVICE_NAME


HA_CALL_SERVICE_PARAMETERS = _HOMEASSISTANT_EXTENSION.HA_CALL_SERVICE_PARAMETERS


_WORD_COUNT_EXAMPLE = importlib.import_module(
    "resources.skills.vbot-cli.assets.extensions.word_count"
)


WORD_COUNT_NAME = _WORD_COUNT_EXAMPLE.WORD_COUNT_NAME


WORD_COUNT_DESCRIPTION = _WORD_COUNT_EXAMPLE.WORD_COUNT_DESCRIPTION


WORD_COUNT_PARAMETERS = _WORD_COUNT_EXAMPLE.WORD_COUNT_PARAMETERS


def _mcp_scenario(case_name: str) -> ProbeScenario:
    from resources.extensions.mcp.extension import MCP_DESCRIPTION, MCP_PARAMETERS

    expected_arguments = MCP_CASE_ARGUMENTS[case_name]
    rendered = json.dumps(expected_arguments, ensure_ascii=False, separators=(",", ":"))
    return ProbeScenario(
        "mcp",
        [{"name": "mcp_example", "description": MCP_DESCRIPTION, "parameters": MCP_PARAMETERS}],
        _probe_messages(
            f"Call mcp_example exactly once with exactly these arguments: {rendered}. "
            "Preserve every value and omit all other fields. This is a test-owned fixture."
        ),
        "mcp_example",
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _ha_list_entities_scenario(case_name: str) -> ProbeScenario:
    arguments_by_case: dict[str, dict[str, Any]] = {
        "default": {},
        "domain": {"domain": "light"},
        "area": {"area": "Living Room"},
        "all": {"domain": "climate", "area": "Upstairs"},
    }
    expected_arguments = arguments_by_case[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {HA_LIST_ENTITIES_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value; do not add any field."
    )
    return ProbeScenario(
        "ha_list_entities",
        [
            {
                "name": HA_LIST_ENTITIES_NAME,
                "description": HA_LIST_ENTITIES_DESCRIPTION,
                "parameters": HA_LIST_ENTITIES_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        HA_LIST_ENTITIES_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _ha_get_state_scenario(case_name: str) -> ProbeScenario:
    arguments_by_case = {
        "light": {"entity_id": "light.living_room"},
        "sensor": {"entity_id": "sensor.outdoor_temperature"},
    }
    expected_arguments = arguments_by_case[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {HA_GET_STATE_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve the value; do not add any field."
    )
    return ProbeScenario(
        "ha_get_state",
        [
            {
                "name": HA_GET_STATE_NAME,
                "description": HA_GET_STATE_DESCRIPTION,
                "parameters": HA_GET_STATE_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        HA_GET_STATE_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _ha_list_services_scenario(case_name: str) -> ProbeScenario:
    arguments_by_case: dict[str, dict[str, Any]] = {
        "default": {},
        "domain": {"domain": "climate"},
    }
    expected_arguments = arguments_by_case[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {HA_LIST_SERVICES_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value; do not add any field."
    )
    return ProbeScenario(
        "ha_list_services",
        [
            {
                "name": HA_LIST_SERVICES_NAME,
                "description": HA_LIST_SERVICES_DESCRIPTION,
                "parameters": HA_LIST_SERVICES_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        HA_LIST_SERVICES_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _ha_call_service_scenario(case_name: str) -> ProbeScenario:
    base = {"domain": "light", "service": "turn_on"}
    arguments_by_case: dict[str, dict[str, Any]] = {
        "base": base,
        "entity": {**base, "entity_id": "light.living_room"},
        "empty_data": {**base, "data": {}},
        "data": {**base, "data": {"brightness": 180, "transition": 2.5}},
        "all": {
            **base,
            "entity_id": "light.living_room",
            "data": {"brightness": 180, "rgb_color": [255, 120, 40]},
        },
    }
    expected_arguments = arguments_by_case[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {HA_CALL_SERVICE_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and nested item; do not add "
        "any field."
    )
    return ProbeScenario(
        "ha_call_service",
        [
            {
                "name": HA_CALL_SERVICE_NAME,
                "description": HA_CALL_SERVICE_DESCRIPTION,
                "parameters": HA_CALL_SERVICE_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        HA_CALL_SERVICE_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _word_count_scenario(case_name: str) -> ProbeScenario:
    arguments_by_case = {
        "plain": {"text": "Count these three words"},
        "empty": {"text": ""},
        "unicode_multiline": {"text": "Grüße aus Berlin\nzweite Zeile 🙂"},
    }
    expected_arguments = arguments_by_case[case_name]
    rendered_arguments = json.dumps(expected_arguments, ensure_ascii=False, separators=(",", ":"))
    instruction = (
        f"Call {WORD_COUNT_NAME} exactly once with exactly this JSON object as its arguments: "
        f"{rendered_arguments}. Preserve every character and line break; do not add any field."
    )
    return ProbeScenario(
        "word_count",
        [
            {
                "name": WORD_COUNT_NAME,
                "description": WORD_COUNT_DESCRIPTION,
                "parameters": WORD_COUNT_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        WORD_COUNT_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )
