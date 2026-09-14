"""Cross-Tool normalization inventory and real Skill probe regression tests."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from core.tools.contracts import compile_tool_contract
from scripts.provider_probe.choices import PROBE_SCENARIOS
from scripts.provider_probe.scenarios import _scenario
from scripts.provider_probe.workflow_tolerance import _skill_case, skill_tolerance_cases
from tests.scripts.provider_probe_helpers import PROBE


def _cases():
    parser = PROBE._parser()
    for action in parser._actions:
        if not action.dest.endswith("_case") or not action.choices:
            continue
        tool = action.dest.removesuffix("_case")
        if tool not in PROBE_SCENARIOS or tool in {"computer", "mcp_workflow"}:
            continue
        for choice in action.choices:
            args = parser.parse_args(["--scenario", tool, action.option_strings[0], choice])
            scenario = _scenario(args)
            if scenario.expected_arguments is not None:
                yield f"{tool}/{choice}", scenario


CASES = list(_cases())


def _mistakes(value: Any, schema: dict[str, Any]) -> Any:
    if not schema:
        return value
    for keyword in ("oneOf", "anyOf"):
        for branch in schema.get(keyword, []):
            if branch.get("type") == "object" and isinstance(value, dict):
                schema = branch
                break
    if isinstance(value, dict):
        fields = schema.get("properties", {})
        return {key: _mistakes(item, fields.get(key, {})) for key, item in value.items()}
    if isinstance(value, list):
        converted = [_mistakes(item, schema.get("items", {})) for item in value]
        return converted[0] if len(converted) == 1 else converted
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value}.0"
    return value


@pytest.mark.parametrize(("name", "scenario"), CASES, ids=[name for name, _ in CASES])
def test_production_definitions_recover_equivalent_value_encodings(name, scenario) -> None:
    definition = next(tool for tool in scenario.tools if tool["name"] == scenario.primary_tool_name)
    contract = compile_tool_contract(
        name=definition["name"], input_schema=definition["parameters"], require_closed_input=False
    )
    original = copy.deepcopy(scenario.expected_arguments)
    expected = original
    if not contract.input_validator.is_valid(expected):
        # Intentional invalid-operation cases belong to owner rejection tests.
        return
    mistaken = _mistakes(original, definition["parameters"])
    assert contract.normalize_arguments(mistaken) == expected, name
    assert scenario.expected_arguments == original


class _Adapter:
    def __init__(self, arguments):
        self.arguments = arguments
        self.calls = 0

    async def send(self, messages, **kwargs):
        self.calls += 1
        if self.calls > 1:
            return {"content": "Done."}
        return {
            "tool_calls": [{"id": "probe", "name": "skill_manage", "arguments": self.arguments}]
        }

    def normalize_response(self, raw, **kwargs):
        return raw


def test_skill_probe_checks_real_files_and_not_just_model_claims() -> None:
    args = PROBE._parser().parse_args([])
    for case in skill_tolerance_cases():
        if "arguments" in case:
            row = asyncio.run(_skill_case(_Adapter(case["arguments"]), args, case))
            assert row["passed"], row
    case = next(case for case in skill_tolerance_cases() if case["id"] == "natural_empty")
    row = asyncio.run(
        _skill_case(
            _Adapter(
                {
                    "action": "write_file",
                    "name": "provider-probe",
                    "file_path": "assets/placeholder.txt",
                    "content": "wrong",
                }
            ),
            args,
            case,
        )
    )
    assert not row["passed"]


def test_cron_probe_verifies_persisted_effects_for_every_case() -> None:
    from scripts.provider_probe.workflow_cron_tolerance import cron_case, cron_tolerance_cases

    class Adapter:
        async def send(self, messages, **kwargs):
            arguments = json.loads(
                messages[-1]["content"]
                .split("\n", 1)[0]
                .removeprefix("Make one Tool Call with these arguments: ")
            )
            return {"tool_calls": [{"id": "fixture", "name": "cron", "arguments": arguments}]}

        def normalize_response(self, raw, **kwargs):
            return raw

    args = PROBE._parser().parse_args([])
    for case in cron_tolerance_cases():
        row = asyncio.run(cron_case(Adapter(), args, case))
        assert row["passed"], row


def test_channel_probe_checks_actual_receiver_and_saved_notes() -> None:
    from scripts.provider_probe.workflow_channel_tolerance import (
        channel_case,
        channel_tolerance_cases,
    )

    class Adapter:
        async def send(self, messages, **kwargs):
            arguments = json.loads(messages[-1]["content"].removeprefix("Arguments: "))
            return {
                "tool_calls": [{"id": "fixture", "name": "channel_send", "arguments": arguments}]
            }

        def normalize_response(self, raw, **kwargs):
            return raw

    args = PROBE._parser().parse_args([])
    for case in channel_tolerance_cases():
        row = asyncio.run(channel_case(Adapter(), args, case))
        assert row["passed"], row


def test_web_probe_checks_fetched_content_and_no_fetch_on_conflicts() -> None:
    from scripts.provider_probe.workflow_web_tolerance import web_case, web_tolerance_cases

    class Adapter:
        async def send(self, messages, **kwargs):
            arguments = json.loads(messages[-1]["content"].removeprefix("Arguments: "))
            return {"tool_calls": [{"id": "fixture", "name": "web_fetch", "arguments": arguments}]}

        def normalize_response(self, raw, **kwargs):
            return raw

    args = PROBE._parser().parse_args([])
    for case in web_tolerance_cases():
        row = asyncio.run(web_case(Adapter(), args, case))
        assert row["passed"], row


def test_ha_probe_checks_actual_service_target_and_payload() -> None:
    from scripts.provider_probe.workflow_ha_tolerance import ha_case, ha_tolerance_cases

    class Adapter:
        async def send(self, messages, **kwargs):
            arguments = json.loads(messages[-1]["content"].removeprefix("Arguments: "))
            return {
                "tool_calls": [
                    {"id": "fixture", "name": kwargs["tools"][0]["name"], "arguments": arguments}
                ]
            }

        def normalize_response(self, raw, **kwargs):
            return raw

    args = PROBE._parser().parse_args([])
    for name in ("ha_call_service", "ha_get_state", "ha_list_entities", "ha_list_services"):
        args.tolerance_tool = name
        for case in ha_tolerance_cases(name):
            row = asyncio.run(ha_case(Adapter(), args, case))
            assert row["passed"], row


def test_status_probe_verifies_resolved_session_targets() -> None:
    from scripts.provider_probe.workflow_status_tolerance import status_case, status_tolerance_cases

    class Adapter:
        async def send(self, messages, **kwargs):
            arguments = json.loads(messages[-1]["content"].removeprefix("Arguments: "))
            return {"tool_calls": [{"id": "fixture", "name": "status", "arguments": arguments}]}

        def normalize_response(self, raw, **kwargs):
            return raw

    args = PROBE._parser().parse_args([])
    for case in status_tolerance_cases():
        row = asyncio.run(status_case(Adapter(), args, case))
        assert row["passed"], row


def test_search_probe_checks_provider_query_filters_and_results() -> None:
    from scripts.provider_probe.workflow_search_tolerance import search_case, search_tolerance_cases

    class Adapter:
        async def send(self, messages, **kwargs):
            arguments = json.loads(messages[-1]["content"].removeprefix("Arguments: "))
            return {"tool_calls": [{"id": "fixture", "name": "web_search", "arguments": arguments}]}

        def normalize_response(self, raw, **kwargs):
            return raw

    args = PROBE._parser().parse_args([])
    for case in search_tolerance_cases():
        row = asyncio.run(search_case(Adapter(), args, case))
        assert row["passed"], row


def test_file_probe_verifies_target_content_and_read_guard() -> None:
    from scripts.provider_probe.workflow_file_tolerance import file_case, file_tolerance_cases

    class Adapter:
        async def send(self, messages, **kwargs):
            arguments = json.loads(messages[-1]["content"].removeprefix("Arguments: "))
            return {"tool_calls": [{"id": "fixture", "name": "write", "arguments": arguments}]}

        def normalize_response(self, raw, **kwargs):
            return raw

    args = PROBE._parser().parse_args([])
    for case in file_tolerance_cases():
        row = asyncio.run(file_case(Adapter(), args, case))
        assert row["passed"], row


def test_skill_read_probe_checks_actual_package_files() -> None:
    from scripts.provider_probe.workflow_skill_read_tolerance import (
        skill_read_case,
        skill_read_cases,
    )

    class Adapter:
        async def send(self, messages, **kwargs):
            arguments = json.loads(messages[-1]["content"].removeprefix("Arguments: "))
            return {"tool_calls": [{"id": "fixture", "name": "skill", "arguments": arguments}]}

        def normalize_response(self, raw, **kwargs):
            return raw

    args = PROBE._parser().parse_args([])
    for case in skill_read_cases():
        row = asyncio.run(skill_read_case(Adapter(), args, case))
        assert row["passed"], row


def test_web_probe_retains_failed_dispatch_evidence() -> None:
    from scripts.provider_probe.workflow_web_tolerance import web_case, web_tolerance_cases

    class Adapter(_Adapter):
        async def send(self, messages, **kwargs):
            return {
                "tool_calls": [{"id": "fixture", "name": "web_fetch", "arguments": self.arguments}]
            }

    row = asyncio.run(
        web_case(
            Adapter({"url": "https://example.com/fixture", "output": "unsupported"}),
            PROBE._parser().parse_args([]),
            web_tolerance_cases()[0],
        )
    )
    assert not row["passed"]
    assert not row["results"][0]["ok"]
    assert row["observed"][0]["arguments"]["output"] == "unsupported"
