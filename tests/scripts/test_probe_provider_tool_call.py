"""Provider diagnostic probe main coverage."""

from __future__ import annotations

import json
from argparse import Namespace
from typing import cast

import scripts.provider_probe.common as probe_common
import scripts.provider_probe.measurements as probe_measurements
import scripts.provider_probe.scenarios as probe_scenarios
import scripts.provider_probe.trace as probe_trace
import scripts.provider_probe.transport as probe_transport
from core.runtime import Runtime


def test_messages_from_wire_restores_internal_assistant_tool_call_shape() -> None:
    messages = probe_trace._messages_from_wire(
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

    assistant = probe_trace._partial_assistant_from_trace(trace)

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
    contracts = probe_measurements._compile_probe_contracts(tools)

    valid = probe_measurements._validation_measurements(
        [{"name": "inspect_probe", "arguments": {"count": 7}}],
        contracts,
    )
    invalid = probe_measurements._validation_measurements(
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
    openai = probe_transport._expected_profile(
        Namespace(profile="auto", provider="openai", wire="openai")
    )
    anthropic = probe_transport._expected_profile(
        Namespace(profile="auto", provider="anthropic", wire="anthropic")
    )
    opencode_go = probe_transport._expected_profile(
        Namespace(profile="auto", provider="opencode-go", wire="openai")
    )

    assert openai == "explicit_non_strict"
    assert anthropic == "omit_strict"
    assert opencode_go == "omit_strict"


def test_probe_stream_groups_tool_fragments_by_slot_when_id_is_only_sent_once() -> None:
    first_fragment = {"slot": 0, "id": "call-1"}
    later_fragment = {"slot": 0}

    assert probe_transport._probe_tool_call_stream_key(first_fragment) == "index:0"
    assert probe_transport._probe_tool_call_stream_key(later_fragment) == "index:0"


def test_nested_operation_scenario_compiles_and_validates() -> None:
    scenario = probe_scenarios._scenario(
        Namespace(scenario="nested_operation", lines=8),
    )
    contracts = probe_measurements._compile_probe_contracts(scenario.tools)

    result = probe_measurements._validation_measurements(
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
    without_defaults = probe_scenarios._scenario(
        Namespace(scenario="optional_booleans", optional_case="omit", lines=8),
    )
    with_defaults = probe_scenarios._scenario(
        Namespace(
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


def test_bare_optional_boolean_scenario_omits_schema_defaults() -> None:
    scenario = probe_scenarios._scenario(
        Namespace(scenario="optional_booleans_bare", optional_case="omit", lines=8),
    )

    properties = scenario.tools[0]["parameters"]["properties"]
    assert "default" not in properties["include_links"]
    assert "default" not in properties["raw"]


def test_optional_boolean_measurements_report_presence_without_argument_values() -> None:
    scenario = probe_scenarios._scenario(
        Namespace(scenario="optional_booleans", optional_case="omit", lines=8),
    )

    result = probe_measurements._optional_boolean_measurements(
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

    probe_common._start_probe_runtime(cast(Runtime, runtime))

    assert runtime.started is True
    assert runtime.background_starts == 0
