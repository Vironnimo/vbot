"""Provider Tool probe: measurements."""

from __future__ import annotations

import json
import re
from typing import Any

from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract
from scripts.provider_probe.common import ProbeScenario


def _argument_measurements(tool_calls: Any) -> tuple[int, int, int]:
    if not isinstance(tool_calls, list):
        return 0, 0, 0
    call_count = 0
    argument_chars = 0
    content_chars = 0
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        call_count += 1
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            continue
        argument_chars += len(json.dumps(arguments, ensure_ascii=False))
        content = arguments.get("content")
        if isinstance(content, str):
            content_chars += len(content)
    return call_count, argument_chars, content_chars


def _optional_boolean_measurements(
    tool_calls: Any,
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(tools) != 1:
        return {}
    parameters = tools[0].get("parameters")
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    if not isinstance(properties, dict) or not {
        "url",
        "include_links",
        "raw",
    }.issubset(properties):
        return {}

    measurements: list[dict[str, Any]] = []
    if isinstance(tool_calls, list):
        for index, call in enumerate(tool_calls, start=1):
            arguments = call.get("arguments") if isinstance(call, dict) else None
            if not isinstance(arguments, dict):
                measurements.append(
                    {
                        "call": index,
                        "url": "invalid",
                        "include_links": "invalid",
                        "raw": "invalid",
                        "unexpected_fields": None,
                    }
                )
                continue
            measurements.append(
                {
                    "call": index,
                    "url": "present" if isinstance(arguments.get("url"), str) else "invalid",
                    "include_links": _boolean_argument_state(arguments, "include_links"),
                    "raw": _boolean_argument_state(arguments, "raw"),
                    "unexpected_fields": len(set(arguments) - {"url", "include_links", "raw"}),
                }
            )
    return {"optional_boolean_calls": measurements}


def _boolean_argument_state(arguments: dict[str, Any], name: str) -> str:
    if name not in arguments:
        return "omitted"
    value = arguments[name]
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "invalid"


def _compile_probe_contracts(
    tools: list[dict[str, Any]],
    *,
    require_closed_input: bool = True,
) -> dict[str, ToolContract]:
    contracts: dict[str, ToolContract] = {}
    for tool in tools:
        name = tool.get("name")
        parameters = tool.get("parameters")
        if not isinstance(name, str) or not isinstance(parameters, dict):
            raise ValueError("probe Tool definitions require name and parameters")
        contracts[name] = compile_tool_contract(
            name=name,
            input_schema=parameters,
            require_closed_input=require_closed_input,
        )
    return contracts


def _expected_argument_measurements(
    tool_calls: Any,
    scenario: ProbeScenario,
) -> dict[str, Any]:
    expected = scenario.expected_arguments
    if expected is None:
        return {}
    if not isinstance(tool_calls, list) or len(tool_calls) != 1:
        return {
            "expected_arguments_match": False,
            "expected_call_count": 1,
            "actual_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
            "missing_expected_fields": sorted(expected),
            "unexpected_fields": [],
            "mismatched_fields": [],
        }
    call = tool_calls[0]
    arguments = call.get("arguments") if isinstance(call, dict) else None
    if not isinstance(arguments, dict):
        return {
            "expected_arguments_match": False,
            "expected_call_count": 1,
            "actual_call_count": 1,
            "missing_expected_fields": sorted(expected),
            "unexpected_fields": [],
            "mismatched_fields": [],
        }
    expected_keys = set(expected)
    actual_keys = set(arguments)
    mismatched = sorted(
        key for key in expected_keys & actual_keys if arguments[key] != expected[key]
    )
    missing = sorted(expected_keys - actual_keys)
    unexpected = sorted(actual_keys - expected_keys)
    tool_name_matches = call.get("name") == scenario.primary_tool_name
    return {
        "expected_arguments_match": (
            tool_name_matches and not missing and not unexpected and not mismatched
        ),
        "expected_call_count": 1,
        "actual_call_count": 1,
        "missing_expected_fields": missing,
        "unexpected_fields": unexpected,
        "mismatched_fields": mismatched,
    }


def _validation_measurements(
    tool_calls: Any,
    contracts: dict[str, ToolContract],
) -> dict[str, Any]:
    if not isinstance(tool_calls, list) or not tool_calls:
        return {
            "schema_valid": False,
            "validation_path": None,
            "validation_keyword": None,
            "validation_error_class": "missing_tool_call",
        }
    for call in tool_calls:
        if not isinstance(call, dict):
            return _invalid_measurement("invalid_call_shape")
        name = call.get("name")
        arguments = call.get("arguments")
        if not isinstance(name, str) or name not in contracts:
            return _invalid_measurement("unknown_tool")
        if not isinstance(arguments, dict):
            return _invalid_measurement("arguments_not_object")
        try:
            contracts[name].validate_arguments(arguments)
        except ToolContractError as error:
            path, keyword = _validation_location(str(error))
            return {
                "schema_valid": False,
                "validation_path": path,
                "validation_keyword": keyword,
                "validation_error_class": "ToolContractError",
            }
    return {
        "schema_valid": True,
        "validation_path": None,
        "validation_keyword": None,
        "validation_error_class": None,
    }


def _invalid_measurement(error_class: str) -> dict[str, Any]:
    return {
        "schema_valid": False,
        "validation_path": None,
        "validation_keyword": None,
        "validation_error_class": error_class,
    }


def _validation_location(message: str) -> tuple[str | None, str | None]:
    match = re.match(r"^arguments(?P<path>[^:]*):.*\[(?P<keyword>[^\]]+)\]$", message)
    if match is None:
        return None, None
    return match.group("path") or "/", match.group("keyword")
