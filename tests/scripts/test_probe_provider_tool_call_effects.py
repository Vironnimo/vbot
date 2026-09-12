"""Provider diagnostic probe effects coverage."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import scripts.provider_probe.workflow_mcp as probe_workflow_mcp
import scripts.provider_probe.workflow_patch as probe_workflow_patch
import scripts.provider_probe.workflow_terminal as probe_workflow_terminal
from tests.scripts.provider_probe_helpers import PROBE, PROJECT_ROOT


class _PatchAdapter:
    def __init__(self, arguments):
        self.arguments = arguments

    async def send(self, messages, **kwargs):
        from core.tools.apply_patch import APPLY_PATCH_TOOL_PARAMETERS

        self.messages = list(messages)
        assert kwargs["tools"][0]["parameters"] == APPLY_PATCH_TOOL_PARAMETERS
        return {
            "content": "",
            "tool_calls": [{"id": "probe", "name": "apply_patch", "arguments": self.arguments}],
        }

    def normalize_response(self, raw, **kwargs):
        return raw


def test_patch_probe_measures_actual_effects_and_rejects_scope_escape():
    args = PROBE._parser().parse_args(["--scenario", "apply_patch"])
    case = probe_workflow_patch._apply_patch_cases()[0]
    good = _PatchAdapter({"patch": "*** Add File: new.txt\n+hello"})
    assert asyncio.run(probe_workflow_patch._probe_apply_patch_case(good, args, case))["passed"]
    wrong = _PatchAdapter({"patch": "*** Add File: new.txt\n+wrong"})
    assert not asyncio.run(probe_workflow_patch._probe_apply_patch_case(wrong, args, case))[
        "passed"
    ]
    escaped = _PatchAdapter({"patch": "*** Add File: ../escaped.txt\n+no"})
    row = asyncio.run(probe_workflow_patch._probe_apply_patch_case(escaped, args, case))
    assert row["error_codes"] == ["probe_scope_violation"]


def test_patch_probe_executes_all_exact_cases_through_production_registry():
    args = PROBE._parser().parse_args(["--scenario", "apply_patch"])
    for case in probe_workflow_patch._apply_patch_cases():
        if "arguments" in case:
            row = asyncio.run(
                probe_workflow_patch._probe_apply_patch_case(
                    _PatchAdapter(case["arguments"]), args, case
                )
            )
            assert row["passed"], row


def test_patch_natural_probe_has_no_conformance_prompt_or_expected_call():
    args = PROBE._parser().parse_args(["--scenario", "apply_patch"])
    case = next(
        c for c in probe_workflow_patch._apply_patch_cases() if c["id"] == "batch_locations"
    )
    adapter = _PatchAdapter(
        {
            "patch": "*** Update File: settings.txt\n@@\n-timeout=10\n+timeout=20\n"
            "@@\n-retries=1\n+retries=3\n*** Update File: notes.txt\n@@\n"
            "-Status: draft\n+Status: ready"
        }
    )
    assert asyncio.run(probe_workflow_patch._probe_apply_patch_case(adapter, args, case))["passed"]
    assert adapter.messages == [
        {
            "role": "user",
            "content": case["task"]
            + "\nCurrent files and their exact contents:\n"
            + json.dumps(case["before"]),
        }
    ]


def test_patch_recovery_probe_supplies_real_result_and_rejects_replay():
    args = PROBE._parser().parse_args(["--scenario", "apply_patch"])
    case = next(
        c for c in probe_workflow_patch._apply_patch_cases() if c["id"] == "recover_without_replay"
    )
    patch = "*** Update File: one.txt\n@@\n-    timeout = 30\n+    timeout = 60"
    adapter = _PatchAdapter({"patch": patch})
    row = asyncio.run(probe_workflow_patch._probe_apply_patch_case(adapter, args, case))
    assert row["passed"] and row["recovery_ok"]
    result = json.loads(adapter.messages[-1]["content"])
    assert result["data"]["status"] == "partial"
    assert result["data"]["results"][1]["error"]["candidates"]
    replay = _PatchAdapter({"patch": "*** Update File: log.txt\n@@\n+done\n" + patch})
    row = asyncio.run(probe_workflow_patch._probe_apply_patch_case(replay, args, case))
    assert not row["passed"] and not row["recovery_ok"] and not row["effect_ok"]


def test_mcp_workflow_probe_dispatches_discovered_targets_through_real_mcp():
    class Adapter:
        def __init__(self):
            self.step = 0
            self.targets = {}

        async def send(self, messages, **kwargs):
            self.step += 1
            if self.step == 1:
                arguments = {"action": "search"}
            elif self.step == 2:
                result = json.loads(messages[-1]["content"])
                self.targets = {
                    item["name"]: item["target"] for item in result["data"]["preview"]["matches"]
                }
                arguments = {"action": "describe", "target": self.targets["get_scene_info"]}
            elif self.step == 3:
                arguments = {"action": "call", "target": self.targets["get_scene_info"]}
            elif self.step == 4:
                arguments = {"action": "describe", "target": self.targets["execute_blender_code"]}
            elif self.step == 5:
                arguments = {
                    "action": "call",
                    "target": self.targets["execute_blender_code"],
                    "arguments": {"code": "bpy.ops.render.render(write_still=True)"},
                }
            else:
                return {"content": "Completed.", "tool_calls": []}
            return {
                "content": None,
                "tool_calls": [
                    {"id": str(self.step), "name": "mcp_blender", "arguments": arguments}
                ],
            }

        def normalize_response(self, raw, **kwargs):
            return raw

    args = PROBE._parser().parse_args(["--scenario", "mcp_workflow"])
    result = asyncio.run(probe_workflow_mcp._probe_mcp_workflow(Adapter(), args))
    assert result["passed"] is True
    assert result["application_calls"] == ["scene", "code"]
    assert result["invalid_calls"] == 0


def test_patch_probe_rejects_importing_a_different_checkout(monkeypatch):
    import pytest

    from core.tools import apply_patch as patch_module

    args = PROBE._parser().parse_args(["--scenario", "apply_patch"])
    monkeypatch.setattr(
        patch_module, "__file__", str(PROJECT_ROOT / "wrong/core/tools/apply_patch.py")
    )
    with pytest.raises(RuntimeError):
        asyncio.run(probe_workflow_patch._probe_apply_patch(None, args))


class _TerminalAdapter:
    def __init__(self, wrong=False):
        self.wrong = wrong

    async def send(self, messages, **kwargs):
        from core.tools.terminal import TERMINAL_TOOL_PARAMETERS

        assert kwargs["tools"][0]["parameters"] == TERMINAL_TOOL_PARAMETERS
        arguments = json.loads(messages[-1]["content"])
        if self.wrong:
            arguments = {"action": "list"}
        return {"tool_calls": [{"id": "fixture", "name": "terminal", "arguments": arguments}]}

    def normalize_response(self, raw, **kwargs):
        return raw


def test_terminal_probe_dispatches_matrix_and_checks_effects():
    args = PROBE._parser().parse_args(["--scenario", "terminal"])

    async def evaluate():
        for case in probe_workflow_terminal._terminal_cases():
            if "arguments" in case:
                row = await probe_workflow_terminal._probe_terminal_case(
                    _TerminalAdapter(), args, case
                )
                assert row["passed"], row
                assert row["non_strict"]
                assert row["definition_tokens"] <= 900
        wrong = await probe_workflow_terminal._probe_terminal_case(
            _TerminalAdapter(wrong=True),
            args,
            probe_workflow_terminal._terminal_cases()[0],
        )
        assert not wrong["passed"]

    asyncio.run(evaluate())


def test_terminal_shell_task_does_not_activate_coding_agent_skill():
    class Adapter(_TerminalAdapter):
        async def send(self, messages, **kwargs):
            assert [message["role"] for message in messages] == ["user"]
            return {
                "tool_calls": [
                    {"id": "shell", "name": "terminal", "arguments": {"action": "start"}}
                ]
            }

    args = PROBE._parser().parse_args(["--scenario", "terminal"])
    case = next(
        case for case in probe_workflow_terminal._terminal_cases() if case["id"] == "natural_start"
    )
    assert asyncio.run(probe_workflow_terminal._probe_terminal_case(Adapter(), args, case))[
        "passed"
    ]


def test_terminal_continuation_probe_loads_reference_and_uses_requested_target():
    class Adapter(_TerminalAdapter):
        async def send(self, messages, **kwargs):
            skill_root = (
                Path(PROBE.__file__).resolve().parents[1] / "resources/skills/coding-agents"
            )
            assert messages[0]["content"] == (
                (skill_root / "SKILL.md").read_text(encoding="utf-8")
                + "\n\n"
                + (skill_root / "references" / case["reference"]).read_text(encoding="utf-8")
            )
            assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "user"]
            observation = json.loads(messages[-2]["content"])["data"]
            assert messages[-1]["content"] == (
                case["task"]
                .replace("{root}", observation["workdir"])
                .replace("{terminal_id}", observation["terminal_id"])
            )
            arguments = dict(case["expected"])
            if "workdir" in arguments:
                arguments["workdir"] = observation["workdir"]
            if "terminal_id" in arguments:
                arguments["terminal_id"] = observation["terminal_id"]
            if self.wrong:
                arguments = {"action": "list"}
            return {"tool_calls": [{"id": "decision", "name": "terminal", "arguments": arguments}]}

    args = PROBE._parser().parse_args(["--scenario", "terminal"])

    async def evaluate():
        nonlocal case
        for case in probe_workflow_terminal._terminal_cases():
            if "reference" in case:
                assert (await probe_workflow_terminal._probe_terminal_case(Adapter(), args, case))[
                    "passed"
                ]
                assert not (
                    await probe_workflow_terminal._probe_terminal_case(
                        Adapter(wrong=True), args, case
                    )
                )["passed"]

    case = {}
    asyncio.run(evaluate())
