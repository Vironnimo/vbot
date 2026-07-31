"""Tests for the safe Provider Tool Call diagnostic probe."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "scripts" / "probe_provider_tool_call.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("provider_tool_call_probe", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROBE = _load_module()


def test_messages_from_wire_restores_internal_assistant_tool_call_shape() -> None:
    messages = PROBE._messages_from_wire(
        [
            {
                "role": "assistant",
                "content": "Writing now.",
                "reasoning_content": "The plan is ready.",
                "tool_calls": [
                    {
                        "id": "call-write",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": '{"path":"plan.md","content":"Plan"}',
                        },
                    }
                ],
            }
        ]
    )

    assert messages == [
        {
            "role": "assistant",
            "content": "Writing now.",
            "reasoning": "The plan is ready.",
            "tool_calls": [
                {
                    "id": "call-write",
                    "name": "write",
                    "arguments": {"path": "plan.md", "content": "Plan"},
                }
            ],
        }
    ]


def test_partial_assistant_from_trace_ignores_heartbeats_and_joins_model_deltas() -> None:
    trace = {
        "response": {
            "body": (
                'data: {"choices":[{"delta":{"reasoning_content":"Think "}}]}\n\n'
                ": ping - 2026-07-27T10:00:00Z\n\n"
                'data: {"choices":[{"delta":{"reasoning_content":"more"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"Writing now."}}]}\n\n'
            )
        }
    }

    assistant = PROBE._partial_assistant_from_trace(trace)

    assert assistant == {
        "role": "assistant",
        "content": "Writing now.",
        "reasoning": "Think more",
    }


def test_validation_measurements_report_structure_without_echoing_values() -> None:
    marker = "PROBE_SECRET_MARKER"
    tools = [
        {
            "name": "inspect_probe",
            "description": marker,
            "parameters": {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
                "additionalProperties": False,
            },
        }
    ]
    contracts = PROBE._compile_probe_contracts(tools)

    valid = PROBE._validation_measurements(
        [{"name": "inspect_probe", "arguments": {"count": 7}}],
        contracts,
    )
    invalid = PROBE._validation_measurements(
        [{"name": "inspect_probe", "arguments": {"count": marker}}],
        contracts,
    )

    assert valid["schema_valid"] is True
    assert invalid == {
        "schema_valid": False,
        "validation_path": "/count",
        "validation_keyword": "type",
        "validation_error_class": "ToolContractError",
    }
    assert marker not in json.dumps(invalid)


def test_probe_profiles_never_enable_strict_mode() -> None:
    openai = PROBE._expected_profile(
        SimpleNamespace(profile="auto", provider="openai", wire="openai")
    )
    anthropic = PROBE._expected_profile(
        SimpleNamespace(profile="auto", provider="anthropic", wire="anthropic")
    )
    opencode_go = PROBE._expected_profile(
        SimpleNamespace(profile="auto", provider="opencode-go", wire="openai")
    )

    assert openai == "openai_non_strict"
    assert anthropic == "best_effort"
    assert opencode_go == "best_effort"


def test_nested_operation_scenario_compiles_and_validates() -> None:
    scenario = PROBE._scenario(
        SimpleNamespace(scenario="nested_operation", lines=8),
    )
    contracts = PROBE._compile_probe_contracts(scenario.tools)

    result = PROBE._validation_measurements(
        [
            {
                "name": scenario.primary_tool_name,
                "arguments": {"request": {"operation": "inspect", "key": "alpha"}},
            }
        ],
        contracts,
    )

    assert result["schema_valid"] is True


def test_optional_boolean_scenarios_differ_only_by_schema_defaults() -> None:
    without_defaults = PROBE._scenario(
        SimpleNamespace(scenario="optional_booleans", optional_case="omit", lines=8),
    )
    with_defaults = PROBE._scenario(
        SimpleNamespace(
            scenario="optional_booleans_schema_defaults",
            optional_case="omit",
            lines=8,
        ),
    )

    without_schema = without_defaults.tools[0]["parameters"]
    with_schema = with_defaults.tools[0]["parameters"]
    assert without_defaults.messages == with_defaults.messages
    assert "default" not in without_schema["properties"]["include_links"]
    assert "default" not in without_schema["properties"]["raw"]
    assert with_schema["properties"]["include_links"]["default"] is True
    assert with_schema["properties"]["raw"]["default"] is False


def test_bare_optional_boolean_scenario_omits_default_values_from_descriptions() -> None:
    scenario = PROBE._scenario(
        SimpleNamespace(scenario="optional_booleans_bare", optional_case="omit", lines=8),
    )

    properties = scenario.tools[0]["parameters"]["properties"]
    assert "default" not in properties["include_links"]
    assert "default" not in properties["raw"]
    assert "true" not in properties["include_links"]["description"]
    assert "false" not in properties["raw"]["description"]
    assert "otherwise omit the field" in properties["include_links"]["description"]


def test_optional_boolean_measurements_report_presence_without_argument_values() -> None:
    scenario = PROBE._scenario(
        SimpleNamespace(scenario="optional_booleans", optional_case="omit", lines=8),
    )

    result = PROBE._optional_boolean_measurements(
        [
            {"name": scenario.primary_tool_name, "arguments": {"url": "secret-one"}},
            {
                "name": scenario.primary_tool_name,
                "arguments": {"url": "secret-two", "include_links": False},
            },
            {
                "name": scenario.primary_tool_name,
                "arguments": {"url": "secret-three", "raw": True},
            },
        ],
        scenario.tools,
    )

    assert result == {
        "optional_boolean_calls": [
            {
                "call": 1,
                "url": "present",
                "include_links": "omitted",
                "raw": "omitted",
                "unexpected_fields": 0,
            },
            {
                "call": 2,
                "url": "present",
                "include_links": "false",
                "raw": "omitted",
                "unexpected_fields": 0,
            },
            {
                "call": 3,
                "url": "present",
                "include_links": "omitted",
                "raw": "true",
                "unexpected_fields": 0,
            },
        ]
    }
    assert "secret" not in json.dumps(result)


def test_optional_boolean_cases_request_one_exact_argument_shape() -> None:
    expected_fragments = {
        "omit": "Omit both include_links and raw",
        "include_links": "include_links=false. Omit raw",
        "raw": "raw=true. Omit include_links",
        "both": "include_links=false, and raw=true",
    }

    for case_name, expected_fragment in expected_fragments.items():
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="optional_booleans",
                optional_case=case_name,
                lines=8,
            ),
        )

        assert "exactly once" in scenario.messages[1]["content"]
        assert expected_fragment in scenario.messages[1]["content"]


def test_probe_runtime_suppresses_background_service_start_hooks() -> None:
    class RuntimeStub:
        def __init__(self) -> None:
            self.started = False
            self.background_starts = 0

        def _start_process_manager(self) -> None:
            self.background_starts += 1

        def _start_channel_service(self) -> None:
            self.background_starts += 1

        def _start_cron_service(self) -> None:
            self.background_starts += 1

        def _start_provider_usage_service(self) -> None:
            self.background_starts += 1

        def start(self) -> None:
            self._start_process_manager()
            self._start_channel_service()
            self._start_cron_service()
            self._start_provider_usage_service()
            self.started = True

    runtime = RuntimeStub()

    PROBE._start_probe_runtime(runtime)

    assert runtime.started is True
    assert runtime.background_starts == 0
