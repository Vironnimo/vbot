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

    assert openai == "explicit_non_strict"
    assert anthropic == "omit_strict"
    assert opencode_go == "omit_strict"


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


def test_process_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in PROBE.PROCESS_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="process",
                process_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.PROCESS_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.PROCESS_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.PROCESS_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )


def test_process_argument_measurements_report_only_structural_differences() -> None:
    scenario = PROBE._process_scenario("input_omit_omit")
    marker = "DO_NOT_PRINT_THIS_VALUE"
    result = PROBE._expected_argument_measurements(
        [
            {
                "name": PROBE.PROCESS_TOOL_NAME,
                "arguments": {
                    "action": "input",
                    "session_id": marker,
                    "text": "probe input",
                    "eof": False,
                },
            }
        ],
        scenario,
    )

    assert result == {
        "expected_arguments_match": False,
        "expected_call_count": 1,
        "actual_call_count": 1,
        "missing_expected_fields": [],
        "unexpected_fields": ["eof"],
        "mismatched_fields": ["session_id"],
    }
    assert marker not in json.dumps(result)


def test_web_fetch_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in PROBE.WEB_FETCH_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="web_fetch",
                web_fetch_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.WEB_FETCH_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.WEB_FETCH_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.WEB_FETCH_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    default = PROBE._web_fetch_scenario("default")
    assert default.expected_arguments == {"url": "https://example.com/provider-tool-probe"}


def test_skill_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in PROBE.SKILL_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="skill",
                skill_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.SKILL_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.SKILL_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.SKILL_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    activate = PROBE._skill_scenario("activate")
    assert activate.expected_arguments == {"name": "vbot-cli"}


def test_skill_list_scenario_uses_production_schema_and_empty_arguments() -> None:
    scenario = PROBE._scenario(
        SimpleNamespace(
            scenario="skill_list",
            lines=8,
        ),
    )
    contracts = PROBE._compile_probe_contracts(
        scenario.tools,
        require_closed_input=scenario.require_closed_input,
    )

    assert scenario.tools[0]["parameters"] is PROBE.SKILL_LIST_TOOL_PARAMETERS
    assert scenario.expected_arguments == {}
    contracts[PROBE.SKILL_LIST_TOOL_NAME].validate_arguments({})
    assert (
        PROBE._expected_argument_measurements(
            [{"name": PROBE.SKILL_LIST_TOOL_NAME, "arguments": {}}],
            scenario,
        )["expected_arguments_match"]
        is True
    )


def test_write_scenario_uses_production_schema_and_exact_arguments() -> None:
    scenario = PROBE._scenario(SimpleNamespace(scenario="write", lines=8))
    contracts = PROBE._compile_probe_contracts(
        scenario.tools,
        require_closed_input=scenario.require_closed_input,
    )
    arguments = scenario.expected_arguments

    assert scenario.tools[0]["parameters"] is PROBE.WRITE_TOOL_PARAMETERS
    assert arguments == {
        "path": "notes/provider-tool-probe.txt",
        "content": "first line\nsecond line\n",
    }
    contracts[PROBE.WRITE_TOOL_NAME].validate_arguments(arguments)
    assert (
        PROBE._expected_argument_measurements(
            [{"name": PROBE.WRITE_TOOL_NAME, "arguments": arguments}],
            scenario,
        )["expected_arguments_match"]
        is True
    )


def test_edit_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in PROBE.EDIT_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="edit",
                edit_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.EDIT_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.EDIT_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.EDIT_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    default = PROBE._edit_scenario("default")
    assert default.expected_arguments == {
        "path": "src/provider_tool_probe.py",
        "old_string": "value = 1",
        "new_string": "value = 2",
    }


def test_analyze_image_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.ANALYZE_IMAGE_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="analyze_image",
                analyze_image_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.ANALYZE_IMAGE_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.ANALYZE_IMAGE_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.ANALYZE_IMAGE_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )


def test_project_scenario_uses_production_schema_and_exact_arguments() -> None:
    scenario = PROBE._scenario(SimpleNamespace(scenario="project", lines=8))
    contracts = PROBE._compile_probe_contracts(
        scenario.tools,
        require_closed_input=scenario.require_closed_input,
    )
    arguments = scenario.expected_arguments

    assert scenario.tools[0]["parameters"] is PROBE.PROJECT_TOOL_PARAMETERS
    assert arguments == {"project_id": "vbot"}
    contracts[PROBE.PROJECT_TOOL_NAME].validate_arguments(arguments)
    assert (
        PROBE._expected_argument_measurements(
            [{"name": PROBE.PROJECT_TOOL_NAME, "arguments": arguments}],
            scenario,
        )["expected_arguments_match"]
        is True
    )


def test_read_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in PROBE.READ_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="read",
                read_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.READ_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.READ_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.READ_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    path_only = PROBE._read_scenario("path_only")
    assert path_only.expected_arguments == {"path": "src/provider_tool_probe.py"}


def test_glob_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in PROBE.GLOB_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="glob",
                glob_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.GLOB_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.GLOB_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.GLOB_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    default = PROBE._glob_scenario("default")
    assert default.expected_arguments == {"pattern": "**/*.py"}


def test_grep_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in PROBE.GREP_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="grep",
                grep_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.GREP_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.GREP_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.GREP_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    default = PROBE._grep_scenario("default")
    assert default.expected_arguments == {"pattern": "TODO|FIXME"}


def test_web_search_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in PROBE.WEB_SEARCH_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="web_search",
                web_search_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.WEB_SEARCH_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.WEB_SEARCH_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.WEB_SEARCH_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    default = PROBE._web_search_scenario("default")
    assert default.expected_arguments == {"query": "vBot Tool schemas"}


def test_text_to_speech_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.TEXT_TO_SPEECH_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="text_to_speech",
                speech_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.TEXT_TO_SPEECH_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.TEXT_TO_SPEECH_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.TEXT_TO_SPEECH_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )


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
