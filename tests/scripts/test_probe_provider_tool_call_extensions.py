"""Provider diagnostic probe extensions coverage."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace

import scripts.provider_probe.choices as probe_choices
import scripts.provider_probe.computer_cases as probe_computer_cases
import scripts.provider_probe.measurements as probe_measurements
import scripts.provider_probe.scenario_extensions as probe_scenario_extensions
import scripts.provider_probe.scenarios as probe_scenarios
import scripts.provider_probe.workflow_mcp as probe_workflow_mcp
import scripts.provider_probe.workflow_swarm as probe_workflow_swarm
from tests.scripts.provider_probe_helpers import PROBE, expected_arguments


def test_swarm_workflow_rejects_a_claim_without_coordination():
    class Adapter:
        async def send(self, messages, **kwargs):
            assert {item["name"] for item in kwargs["tools"]} == {
                "swarm_board",
                "swarm_inbox",
                "swarm_state",
            }
            return {"content": "Everything is complete."}

        def normalize_response(self, raw, **kwargs):
            return raw

    args = PROBE._parser().parse_args(
        ["--scenario", "swarm_tool", "--swarm-case", "workflow", "--profile", "explicit_non_strict"]
    )
    result = asyncio.run(probe_workflow_swarm._probe_swarm_tool(Adapter(), args))
    assert result["passed"] is False
    assert result["missing_actions"]
    assert result["final_response_received"] is True


def test_swarm_workflow_persists_failed_calls_and_resumes_from_feedback():
    class Adapter:
        step = 0

        async def send(self, messages, **_kwargs):
            results = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
            roster = next(
                (item["data"] for item in results if (item.get("data") or {}).get("self")), {}
            )
            peer = next(
                (
                    row["id"]
                    for row in roster.get("roster", [])
                    if row["id"] != roster.get("self", {}).get("id")
                ),
                "",
            )
            topic = next(
                (
                    row["id"]
                    for item in results
                    for row in (item.get("data") or {}).get("entries", [])
                    if row.get("title") == "Topic"
                ),
                "",
            )
            sequence = [
                ("swarm_state", {}),
                ("swarm_board", {"action": "list"}),
                (
                    "swarm_board",
                    {"action": "create", "title": "invalid", "text": "draft", "extra": True},
                ),
                (
                    "swarm_board",
                    {"action": "post", "text": "I will review clarity", "request_id": "intro"},
                ),
                (
                    "swarm_board",
                    {"action": "post", "text": "I will review clarity", "request_id": "intro"},
                ),
                ("swarm_board", {"action": "join", "discussion_id": topic}),
                (
                    "swarm_board",
                    {
                        "action": "create",
                        "title": "Review",
                        "text": "Check facts, clarity, completeness",
                        "request_id": "review",
                    },
                ),
                (
                    "swarm_board",
                    {
                        "action": "post",
                        "text": "Please review",
                        "recipients": [peer],
                        "request_id": "ping",
                    },
                ),
                ("swarm_inbox", {}),
                ("final", {}),
                ("swarm_inbox", {}),
            ]
            if self.step >= len(sequence):
                return {"content": "complete"}
            name, arguments = sequence[self.step]
            self.step += 1
            if name == "final":
                return {"content": "Draft ready for review"}
            return {
                "tool_calls": [{"id": f"call-{self.step}", "name": name, "arguments": arguments}]
            }

        def normalize_response(self, raw, **_kwargs):
            return raw

    args = PROBE._parser().parse_args(
        ["--scenario", "swarm_tool", "--swarm-case", "workflow", "--profile", "explicit_non_strict"]
    )
    result = asyncio.run(probe_workflow_swarm._probe_swarm_tool(Adapter(), args))
    assert result["passed"]
    assert any(not call["ok"] for call in result["calls"])
    assert result["durable_receipts"] > 0


def test_swarm_probe_uses_registered_handlers_and_canonical_receipts():
    class Adapter:
        async def send(self, messages, **kwargs):
            tool_name = messages[-1]["content"].split()[1]
            assert tool_name in {tool["name"] for tool in kwargs["tools"]}
            arguments = json.loads(messages[-1]["content"].split(": ", 1)[1])
            return {
                "tool_calls": [{"id": "fixture-call", "name": tool_name, "arguments": arguments}]
            }

        def normalize_response(self, raw, **kwargs):
            return raw

    args = PROBE._parser().parse_args(
        ["--scenario", "swarm_tool", "--profile", "explicit_non_strict"]
    )
    result = asyncio.run(probe_workflow_swarm._probe_swarm_tool(Adapter(), args))
    assert result["passed"]
    assert len(result["cases"]) >= 50
    assert result["strict_true_tool_count"] == 0
    args.swarm_case = "bounds"
    bounds = asyncio.run(probe_workflow_swarm._probe_swarm_tool(Adapter(), args))
    assert bounds["passed"]
    assert len(bounds["cases"]) == 8
    args.swarm_case = "all"
    args.swarm_tool = "swarm_inbox"
    inbox = asyncio.run(probe_workflow_swarm._probe_swarm_tool(Adapter(), args))
    assert inbox["passed"]
    assert len(inbox["cases"]) == 15
    args.swarm_tool = "swarm_state"
    state = asyncio.run(probe_workflow_swarm._probe_swarm_tool(Adapter(), args))
    assert state["passed"]
    assert {"status_default", "status_cursor", "name_rejected", "name_field_rejected"} <= {
        row["case"] for row in state["cases"]
    }


def test_mcp_workflow_probe_rejects_an_unsupported_completion_claim():
    class Adapter:
        async def send(self, *args, **kwargs):
            return {"content": "Finished rendering.", "tool_calls": []}

        def normalize_response(self, raw, **kwargs):
            return raw

    args = PROBE._parser().parse_args(["--scenario", "mcp_workflow"])
    result = asyncio.run(probe_workflow_mcp._probe_mcp_workflow(Adapter(), args))
    assert result["passed"] is False
    assert result["application_calls"] == []


def test_word_count_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.WORD_COUNT_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="word_count",
                word_count_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is probe_scenario_extensions.WORD_COUNT_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in scenario.tools[0]["parameters"]
        contracts[probe_scenario_extensions.WORD_COUNT_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [
                    {
                        "name": probe_scenario_extensions.WORD_COUNT_NAME,
                        "arguments": arguments,
                    }
                ],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert probe_scenario_extensions._word_count_scenario("empty").expected_arguments == {
        "text": ""
    }
    assert probe_scenario_extensions._word_count_scenario(
        "unicode_multiline"
    ).expected_arguments == {"text": "Grüße aus Berlin\nzweite Zeile 🙂"}


def test_ha_list_entities_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.HA_LIST_ENTITIES_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="ha_list_entities",
                ha_list_entities_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert (
            scenario.tools[0]["parameters"] is probe_scenario_extensions.HA_LIST_ENTITIES_PARAMETERS
        )
        assert arguments is not None
        assert "additionalProperties" not in scenario.tools[0]["parameters"]
        contracts[probe_scenario_extensions.HA_LIST_ENTITIES_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [
                    {
                        "name": probe_scenario_extensions.HA_LIST_ENTITIES_NAME,
                        "arguments": arguments,
                    }
                ],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert probe_scenario_extensions._ha_list_entities_scenario("default").expected_arguments == {}
    assert probe_scenario_extensions._ha_list_entities_scenario("all").expected_arguments == {
        "domain": "climate",
        "area": "Upstairs",
    }


def test_ha_get_state_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.HA_GET_STATE_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="ha_get_state",
                ha_get_state_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is probe_scenario_extensions.HA_GET_STATE_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in scenario.tools[0]["parameters"]
        contracts[probe_scenario_extensions.HA_GET_STATE_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [
                    {
                        "name": probe_scenario_extensions.HA_GET_STATE_NAME,
                        "arguments": arguments,
                    }
                ],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert probe_scenario_extensions._ha_get_state_scenario("light").expected_arguments == {
        "entity_id": "light.living_room"
    }


def test_ha_list_services_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.HA_LIST_SERVICES_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="ha_list_services",
                ha_list_services_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert (
            scenario.tools[0]["parameters"] is probe_scenario_extensions.HA_LIST_SERVICES_PARAMETERS
        )
        assert arguments is not None
        assert "additionalProperties" not in scenario.tools[0]["parameters"]
        contracts[probe_scenario_extensions.HA_LIST_SERVICES_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [
                    {
                        "name": probe_scenario_extensions.HA_LIST_SERVICES_NAME,
                        "arguments": arguments,
                    }
                ],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert probe_scenario_extensions._ha_list_services_scenario("default").expected_arguments == {}
    assert probe_scenario_extensions._ha_list_services_scenario("domain").expected_arguments == {
        "domain": "climate"
    }


def test_ha_call_service_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.HA_CALL_SERVICE_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="ha_call_service",
                ha_call_service_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert (
            scenario.tools[0]["parameters"] is probe_scenario_extensions.HA_CALL_SERVICE_PARAMETERS
        )
        assert arguments is not None
        assert "additionalProperties" not in scenario.tools[0]["parameters"]
        contracts[probe_scenario_extensions.HA_CALL_SERVICE_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [
                    {
                        "name": probe_scenario_extensions.HA_CALL_SERVICE_NAME,
                        "arguments": arguments,
                    }
                ],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert probe_scenario_extensions._ha_call_service_scenario("base").expected_arguments == {
        "domain": "light",
        "service": "turn_on",
    }
    assert (
        expected_arguments(probe_scenario_extensions._ha_call_service_scenario("empty_data"))[
            "data"
        ]
        == {}
    )
    assert probe_scenario_extensions._ha_call_service_scenario("all").expected_arguments == {
        "domain": "light",
        "service": "turn_on",
        "entity_id": "light.living_room",
        "data": {"brightness": 180, "rgb_color": [255, 120, 40]},
    }


def test_mcp_probe_uses_the_production_definition_for_every_case():
    from resources.extensions.mcp.extension import MCP_DESCRIPTION, MCP_PARAMETERS

    for name, arguments in probe_choices.MCP_CASE_ARGUMENTS.items():
        scenario = probe_scenario_extensions._mcp_scenario(name)
        assert scenario.tools == [
            {"name": "mcp_example", "description": MCP_DESCRIPTION, "parameters": MCP_PARAMETERS}
        ]
        assert scenario.expected_arguments == arguments
        assert scenario.require_closed_input is False


def test_computer_probe_uses_production_definition_and_validates_matrix():
    from resources.extensions.computer_use._arguments import (
        _validate_arguments,
    )
    from resources.extensions.computer_use.extension import (
        COMPUTER_PARAMETERS,
        InvalidComputerArgumentsError,
    )
    from resources.extensions.computer_use.observations import Observation

    for (
        case,
        expected,
    ) in probe_computer_cases.COMPUTER_CASE_ARGUMENTS.items():
        args = PROBE._parser().parse_args(["--scenario", "computer", "--computer-case", case])
        scenario = probe_scenarios._scenario(args)
        assert scenario.tools[0]["parameters"] is COMPUTER_PARAMETERS
        assert scenario.expected_arguments == expected
        try:
            # These cases consume a previously returned window observation.
            reference = (
                Observation(target=("window", 1, 2), foreground=True)
                if case
                in {
                    "capture_view_query",
                    "verify_view",
                    "element_target",
                    "sequence_element_target",
                    "capture_reset_background",
                }
                else None
            )
            _validate_arguments(expected, reference)
        except InvalidComputerArgumentsError:
            assert case.startswith("invalid_")
        else:
            assert not case.startswith("invalid_")


def test_swarm_unassisted_requires_actual_feedback_and_a_later_publication():
    class Adapter:
        def __init__(self, revise):
            self.step = 0
            self.revise = revise

        async def send(self, messages, **_kwargs):
            prompt = next(row["content"] for row in messages if row["role"] == "user")
            assert "swarm_board" not in prompt and "swarm_state" not in prompt
            calls = [
                ("swarm_inbox", {}),
                ("swarm_board", {"action": "post", "text": "Draft", "request_id": "draft"}),
                ("swarm_inbox", {}),
            ]
            if self.revise:
                calls.append(
                    (
                        "swarm_board",
                        {"action": "post", "text": "Revised checklist", "request_id": "revision"},
                    )
                )
            if self.step >= len(calls):
                return {"content": "Finished"}
            name, arguments = calls[self.step]
            self.step += 1
            return {"tool_calls": [{"id": str(self.step), "name": name, "arguments": arguments}]}

        def normalize_response(self, raw, **_kwargs):
            return raw

    args = PROBE._parser().parse_args(
        [
            "--scenario",
            "swarm_tool",
            "--swarm-case",
            "unassisted",
            "--profile",
            "explicit_non_strict",
        ]
    )
    incomplete = asyncio.run(probe_workflow_swarm._probe_swarm_tool(Adapter(False), args))
    assert incomplete["feedback_received"]
    assert not incomplete["passed"]
    complete = asyncio.run(probe_workflow_swarm._probe_swarm_tool(Adapter(True), args))
    assert complete["passed"]
    assert complete["published_after_feedback"]
