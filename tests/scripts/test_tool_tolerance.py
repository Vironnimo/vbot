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
        return {
            key.upper().replace("_", "-") if key in fields else key: _mistakes(
                item, fields.get(key, {})
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        converted = [_mistakes(item, schema.get("items", {})) for item in value]
        return converted[0] if len(converted) == 1 else converted
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value}.0"
    if isinstance(value, str) and value in schema.get("enum", []):
        return " " + value.upper().replace("_", "-") + " "
    return value


@pytest.mark.parametrize(("name", "scenario"), CASES, ids=[name for name, _ in CASES])
def test_production_definitions_recover_equivalent_shapes(name, scenario) -> None:
    definition = next(tool for tool in scenario.tools if tool["name"] == scenario.primary_tool_name)
    contract = compile_tool_contract(
        name=definition["name"], input_schema=definition["parameters"], require_closed_input=False
    )
    original = copy.deepcopy(scenario.expected_arguments)
    expected = contract.normalize_arguments(original)
    if not contract.input_validator.is_valid(expected):
        # Intentional invalid-operation cases belong to owner rejection tests.
        return
    mistaken = {"request": _mistakes(original, definition["parameters"])}
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


def test_edit_probe_executes_mistakes_and_checks_file_effects() -> None:
    from scripts.provider_probe.workflow_tolerance import _edit_case, edit_tolerance_cases

    class Adapter(_Adapter):
        async def send(self, messages, **kwargs):
            result = await super().send(messages, **kwargs)
            for call in result.get("tool_calls", []):
                call["name"] = "edit"
            return result

    args = PROBE._parser().parse_args([])
    for case in edit_tolerance_cases():
        if "arguments" in case:
            row = asyncio.run(_edit_case(Adapter(case["arguments"]), args, case))
            assert row["passed"], row
    case = edit_tolerance_cases()[0]
    wrong = {
        "edits": [
            {"path": "src/provider_tool_probe.py", "old_string": "value = 1", "new_string": "wrong"}
        ]
    }
    assert not asyncio.run(_edit_case(Adapter(wrong), args, case))["passed"]


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
    for name in ("ha_call_service", "ha_get_state"):
        args.tolerance_tool = name
        for case in ha_tolerance_cases(name):
            row = asyncio.run(ha_case(Adapter(), args, case))
            assert row["passed"], row
