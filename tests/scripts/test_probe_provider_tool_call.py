"""Tests for the safe Provider Tool Call diagnostic probe."""

from __future__ import annotations

import asyncio
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
    case = PROBE._apply_patch_cases()[0]
    good = _PatchAdapter({"patch": "*** Add File: new.txt\n+hello"})
    assert asyncio.run(PROBE._probe_apply_patch_case(good, args, case))["passed"]
    wrong = _PatchAdapter({"patch": "*** Add File: new.txt\n+wrong"})
    assert not asyncio.run(PROBE._probe_apply_patch_case(wrong, args, case))["passed"]
    escaped = _PatchAdapter({"patch": "*** Add File: ../escaped.txt\n+no"})
    row = asyncio.run(PROBE._probe_apply_patch_case(escaped, args, case))
    assert row["error_codes"] == ["probe_scope_violation"]


def test_patch_probe_executes_all_exact_cases_through_production_registry():
    args = PROBE._parser().parse_args(["--scenario", "apply_patch"])
    for case in PROBE._apply_patch_cases():
        if "arguments" in case:
            row = asyncio.run(
                PROBE._probe_apply_patch_case(_PatchAdapter(case["arguments"]), args, case)
            )
            assert row["passed"], row


def test_patch_natural_probe_has_no_conformance_prompt_or_expected_call():
    args = PROBE._parser().parse_args(["--scenario", "apply_patch"])
    case = next(c for c in PROBE._apply_patch_cases() if c["id"] == "batch_locations")
    adapter = _PatchAdapter(
        {
            "patch": "*** Update File: settings.txt\n@@\n-timeout=10\n+timeout=20\n"
            "@@\n-retries=1\n+retries=3\n*** Update File: notes.txt\n@@\n"
            "-Status: draft\n+Status: ready"
        }
    )
    assert asyncio.run(PROBE._probe_apply_patch_case(adapter, args, case))["passed"]
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
    case = next(c for c in PROBE._apply_patch_cases() if c["id"] == "recover_without_replay")
    patch = "*** Update File: one.txt\n@@\n-    timeout = 30\n+    timeout = 60"
    adapter = _PatchAdapter({"patch": patch})
    row = asyncio.run(PROBE._probe_apply_patch_case(adapter, args, case))
    assert row["passed"] and row["recovery_ok"]
    result = json.loads(adapter.messages[-1]["content"])
    assert result["data"]["status"] == "partial"
    assert result["data"]["results"][1]["error"]["candidates"]
    replay = _PatchAdapter({"patch": "*** Update File: log.txt\n@@\n+done\n" + patch})
    row = asyncio.run(PROBE._probe_apply_patch_case(replay, args, case))
    assert not row["passed"] and not row["recovery_ok"] and not row["effect_ok"]


class _ReflectionAdapter:
    def __init__(self, calls=()):
        self.calls = iter(calls)
        self.messages = []

    async def send(self, messages, **kwargs):
        self.messages = list(messages)
        assert {tool["name"] for tool in kwargs["tools"]} == {"memory", "skill", "skill_manage"}
        call = next(self.calls, None)
        if call is None:
            return {"content": "Review complete."}
        name, arguments = call
        return {
            "content": "",
            "tool_calls": [{"id": f"probe-{len(messages)}", "name": name, "arguments": arguments}],
        }

    def normalize_response(self, raw, **kwargs):
        return raw


def _reflection_probe(case_id, scope, calls=()):
    case = next(case for case in PROBE._reflection_cases() if case["id"] == case_id)
    args = PROBE._parser().parse_args(["--scenario", "reflection_workflow"])
    adapter = _ReflectionAdapter(calls)
    result = asyncio.run(PROBE._probe_reflection_case(adapter, args, case, scope))
    return result, adapter


def test_reflection_probe_requires_effects_not_completion_claims():
    result, _adapter = _reflection_probe("standing_preference", "combined")
    assert result["final_response_received"] is True
    assert result["effect_ok"] is False
    assert result["passed"] is False


def test_reflection_probe_uses_production_memory_results_and_keeps_expectations_private():
    result, adapter = _reflection_probe(
        "standing_preference",
        "combined",
        [
            ("memory", {"action": "list", "scope": "user"}),
            (
                "memory",
                {"action": "add", "scope": "user", "content": "User prefers German responses."},
            ),
        ],
    )
    assert result["passed"] is True
    results = [
        json.loads(message["content"]) for message in adapter.messages if message["role"] == "tool"
    ]
    assert results[-1]["data"]["entries"][0]["content"] == "User prefers German responses."
    assert "standing_preference" not in json.dumps(adapter.messages)
    assert '"expected"' not in json.dumps(adapter.messages)


def test_reflection_probe_detects_a_write_without_checking_current_memory():
    result, _adapter = _reflection_probe(
        "standing_preference",
        "combined",
        [
            (
                "memory",
                {"action": "add", "scope": "user", "content": "User prefers German responses."},
            ),
        ],
    )
    assert result["effect_ok"] is True
    assert result["passed"] is False
    assert "memory_write_without_current_list" in result["violations"]


def test_reflection_probe_detects_cross_scope_substitution():
    result, _adapter = _reflection_probe(
        "standing_preference",
        "skill",
        [
            ("memory", {"action": "list", "scope": "user"}),
        ],
    )
    assert result["passed"] is False
    assert "tool_call_rejected" in result["violations"]


def test_reflection_probe_accepts_a_noop_after_reading_current_memory():
    result, adapter = _reflection_probe(
        "already_saved_memory",
        "memory",
        [
            ("memory", {"action": "list", "scope": "user"}),
        ],
    )
    assert result["passed"] is True
    assert result["actions"] == []
    assert "German" not in adapter.messages[0]["content"]
    results = [
        json.loads(message["content"]) for message in adapter.messages if message["role"] == "tool"
    ]
    assert "German" in results[0]["data"]["entries"][0]["content"]


def test_reflection_probe_verifies_skill_replacement_and_preservation():
    result, _adapter = _reflection_probe(
        "correct_existing_skill",
        "skill",
        [
            ("skill", {"name": "archive-publishing", "file_path": "SKILL.md"}),
            (
                "skill_manage",
                {
                    "action": "patch",
                    "name": "archive-publishing",
                    "match": "2. Publish it immediately.\n3. Check its checksum after publication.",
                    "content": "2. Verify its checksum.\n3. Publish after verification succeeds.",
                },
            ),
        ],
    )
    assert result["passed"] is True


def test_reflection_probe_rejects_unnecessary_skill_creation():
    result, _adapter = _reflection_probe(
        "routine",
        "combined",
        [
            ("skill", {}),
            (
                "skill_manage",
                {
                    "action": "create",
                    "name": "alphabetizing",
                    "content": (
                        "---\nname: alphabetizing\ndescription: Alphabetize labels.\n---\n"
                        "Sort the labels alphabetically.\n"
                    ),
                },
            ),
        ],
    )
    assert result["actions"][0]["ok"] is True
    assert result["effect_ok"] is False
    assert result["passed"] is False


def test_reflection_probe_rejects_writing_a_protected_skill():
    result, _adapter = _reflection_probe(
        "protected_skill",
        "combined",
        [
            ("skill", {"name": "archive-publishing", "file_path": "SKILL.md"}),
            (
                "skill_manage",
                {
                    "action": "patch",
                    "name": "archive-publishing",
                    "match": "Publish it immediately.",
                    "content": "Verify before publishing.",
                },
            ),
        ],
    )
    assert result["actions"][0]["ok"] is False
    assert result["passed"] is False


def test_reflection_probe_retains_other_results_after_one_case_fails(monkeypatch):
    monkeypatch.setattr(
        PROBE,
        "_reflection_cases",
        lambda: [
            {"id": "fails", "expected": {"memory": {}}},
            {"id": "finishes", "expected": {"skill": {}}},
        ],
    )

    async def evaluate(_adapter, _args, case, scope):
        if case["id"] == "fails":
            raise ValueError("private diagnostic must not be printed")
        return {"case": case["id"], "scope": scope, "passed": True}

    monkeypatch.setattr(PROBE, "_probe_reflection_case", evaluate)
    args = PROBE._parser().parse_args(["--scenario", "reflection_workflow"])
    result = asyncio.run(PROBE._probe_reflection_workflow(None, args))
    assert result["passed"] is False
    assert result["cases"] == [
        {"case": "fails", "scope": "memory", "passed": False, "error_type": "ValueError"},
        {"case": "finishes", "scope": "skill", "passed": True},
    ]


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
    result = asyncio.run(PROBE._probe_swarm_tool(Adapter(), args))
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
    result = asyncio.run(PROBE._probe_swarm_tool(Adapter(), args))
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
    result = asyncio.run(PROBE._probe_swarm_tool(Adapter(), args))
    assert result["passed"]
    assert len(result["cases"]) >= 50
    assert result["strict_true_tool_count"] == 0
    args.swarm_case = "bounds"
    bounds = asyncio.run(PROBE._probe_swarm_tool(Adapter(), args))
    assert bounds["passed"]
    assert len(bounds["cases"]) == 8
    args.swarm_case = "all"
    args.swarm_tool = "swarm_inbox"
    inbox = asyncio.run(PROBE._probe_swarm_tool(Adapter(), args))
    assert inbox["passed"]
    assert len(inbox["cases"]) == 15
    args.swarm_tool = "swarm_state"
    state = asyncio.run(PROBE._probe_swarm_tool(Adapter(), args))
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
    result = asyncio.run(PROBE._probe_mcp_workflow(Adapter(), args))
    assert result["passed"] is False
    assert result["application_calls"] == []


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
    result = asyncio.run(PROBE._probe_mcp_workflow(Adapter(), args))
    assert result["passed"] is True
    assert result["application_calls"] == ["scene", "code"]
    assert result["invalid_calls"] == 0


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


def test_probe_stream_groups_tool_fragments_by_slot_when_id_is_only_sent_once() -> None:
    first_fragment = {"slot": 0, "id": "call-1"}
    later_fragment = {"slot": 0}

    assert PROBE._probe_tool_call_stream_key(first_fragment) == "index:0"
    assert PROBE._probe_tool_call_stream_key(later_fragment) == "index:0"


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


def test_bare_optional_boolean_scenario_omits_schema_defaults() -> None:
    scenario = PROBE._scenario(
        SimpleNamespace(scenario="optional_booleans_bare", optional_case="omit", lines=8),
    )

    properties = scenario.tools[0]["parameters"]["properties"]
    assert "default" not in properties["include_links"]
    assert "default" not in properties["raw"]


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
        assert set(arguments) <= {"action", "process_id", "filter", "limit", "before"}
        assert arguments["action"] in {"status", "kill"}
        contracts[PROBE.PROCESS_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.PROCESS_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )


def test_process_argument_measurements_report_only_structural_differences() -> None:
    scenario = PROBE._process_scenario("status_one")
    marker = "DO_NOT_PRINT_THIS_VALUE"
    result = PROBE._expected_argument_measurements(
        [
            {
                "name": PROBE.PROCESS_TOOL_NAME,
                "arguments": {
                    "action": "status",
                    "process_id": marker,
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
        "mismatched_fields": ["process_id"],
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
    catalog = PROBE._skill_scenario("list")
    assert catalog.expected_arguments == {}


def test_skill_manage_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.SKILL_MANAGE_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="skill_manage",
                skill_manage_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.SKILL_MANAGE_TOOL_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in json.dumps(scenario.tools[0]["parameters"])
        assert "oneOf" not in scenario.tools[0]["parameters"]
        assert '"default"' not in json.dumps(scenario.tools[0]["parameters"])
        contracts[PROBE.SKILL_MANAGE_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.SKILL_MANAGE_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    create = PROBE._skill_manage_scenario("create_own")
    patch = PROBE._skill_manage_scenario("patch_default")
    assert "scope" not in create.expected_arguments
    assert "file_path" not in patch.expected_arguments
    assert "replace_all" not in patch.expected_arguments
    assert PROBE._skill_manage_scenario("write_asset_empty").expected_arguments["content"] == ""


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


def test_word_count_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.WORD_COUNT_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="word_count",
                word_count_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.WORD_COUNT_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in scenario.tools[0]["parameters"]
        contracts[PROBE.WORD_COUNT_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.WORD_COUNT_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert PROBE._word_count_scenario("empty").expected_arguments == {"text": ""}
    assert PROBE._word_count_scenario("unicode_multiline").expected_arguments == {
        "text": "Grüße aus Berlin\nzweite Zeile 🙂"
    }


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
        "edits": [
            {
                "path": "src/provider_tool_probe.py",
                "old_string": "value = 1",
                "new_string": "value = 2",
            }
        ]
    }
    assert len(PROBE._edit_scenario("multi_file").expected_arguments["edits"]) == 2
    same_file_edits = PROBE._edit_scenario("same_file_sequence").expected_arguments["edits"]
    assert [edit["path"] for edit in same_file_edits] == [
        "src/provider_tool_probe.py",
        "src/provider_tool_probe.py",
    ]
    assert same_file_edits[1]["old_string"] == same_file_edits[0]["new_string"]


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


def test_bash_cases_use_production_profiles_and_exact_arguments() -> None:
    for case_name in PROBE.BASH_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="bash",
                bash_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments
        parameters = scenario.tools[0]["parameters"]

        assert arguments is not None
        assert "additionalProperties" not in json.dumps(parameters)
        assert "oneOf" not in parameters
        contracts[PROBE.BASH_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.BASH_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    top = PROBE._bash_scenario("top_auto_default")
    sub = PROBE._bash_scenario("sub_auto_default")
    assert top.tools[0]["parameters"] is PROBE.BASH_TOOL_PARAMETERS
    assert PROBE._bash_scenario("top_foreground_default").expected_arguments == {
        "command": "python --version"
    }
    assert PROBE._bash_scenario("sub_foreground_default").expected_arguments == {
        "command": "python --version"
    }
    assert top.expected_arguments == {
        "mode": "auto",
        "command": "python -m pytest tests/core/tools/test_bash.py -q",
    }
    assert PROBE._bash_scenario("top_foreground_env_one").expected_arguments == {
        "mode": "foreground",
        "command": "python -c \"import os; print(bool(os.environ['OPENAI_API_KEY']))\"",
        "env_keys": ["OPENAI_API_KEY"],
    }
    assert PROBE._bash_scenario("top_foreground_env_many").expected_arguments == {
        "mode": "foreground",
        "command": 'python -c "import os; print(len(os.environ))"',
        "env_keys": ["OPENAI_API_KEY", "OPENROUTER_API_KEY"],
    }
    top_contracts = PROBE._compile_probe_contracts(
        top.tools,
        require_closed_input=top.require_closed_input,
    )
    for env_keys, keyword in (
        ([], "minItems"),
        ([""], "minLength"),
        (["OPENAI_API_KEY", "OPENAI_API_KEY"], "uniqueItems"),
    ):
        validation = PROBE._validation_measurements(
            [
                {
                    "name": PROBE.BASH_TOOL_NAME,
                    "arguments": {
                        "mode": "foreground",
                        "command": "python --version",
                        "env_keys": env_keys,
                    },
                }
            ],
            top_contracts,
        )
        assert validation["schema_valid"] is False
        assert validation["validation_path"].startswith("/env_keys")
        assert validation["validation_keyword"] == keyword
    assert top.tools[0]["parameters"]["properties"]["background_after_seconds"]["default"] == 30
    assert sub.tools[0]["parameters"]["properties"]["mode"]["enum"] == [
        "foreground",
        "auto",
    ]
    assert sub.tools[0]["parameters"]["properties"]["background_after_seconds"]["default"] == 1800


def test_channel_send_cases_use_production_profiles_and_exact_arguments() -> None:
    for case_name in PROBE.CHANNEL_SEND_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="channel_send",
                channel_send_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert arguments is not None
        assert "additionalProperties" not in json.dumps(scenario.tools[0]["parameters"])
        assert "oneOf" not in scenario.tools[0]["parameters"]
        contracts[PROBE.CHANNEL_SEND_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.CHANNEL_SEND_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    telegram = PROBE._channel_send_scenario("telegram_message")
    discord = PROBE._channel_send_scenario("discord_message")
    mixed = PROBE._channel_send_scenario("mixed_telegram_button")
    assert "buttons" in telegram.tools[0]["parameters"]["properties"]
    assert "buttons" not in discord.tools[0]["parameters"]["properties"]
    assert mixed.tools[0]["parameters"]["properties"]["channel_id"]["enum"] == [
        "discord-probe",
        "telegram-probe",
    ]


def test_calendar_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.CALENDAR_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="calendar",
                calendar_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.CALENDAR_TOOL_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in json.dumps(scenario.tools[0]["parameters"])
        assert "oneOf" not in scenario.tools[0]["parameters"]
        contracts[PROBE.CALENDAR_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.CALENDAR_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert PROBE._calendar_scenario("list_default").expected_arguments == {"action": "list"}
    assert PROBE._calendar_scenario("create_timed").expected_arguments == {
        "action": "create",
        "title": "Dentist",
        "start": "2026-09-10T15:00",
    }
    assert PROBE._calendar_scenario("update_stop_repeat").expected_arguments == {
        "action": "update",
        "id": "event-123",
        "rrule": None,
    }


def test_cron_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.CRON_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="cron",
                cron_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.CRON_TOOL_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in json.dumps(scenario.tools[0]["parameters"])
        assert "oneOf" not in scenario.tools[0]["parameters"]
        contracts[PROBE.CRON_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.CRON_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert PROBE._cron_scenario("list").expected_arguments == {"action": "list"}
    assert PROBE._cron_scenario("create_cron").expected_arguments == {
        "action": "create",
        "prompt": "Prepare the daily operations summary.",
        "schedule": "0 9 * * *",
    }
    assert PROBE._cron_scenario("update_repeat_null").expected_arguments["repeat"] is None


def test_history_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.HISTORY_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="history",
                history_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.HISTORY_TOOL_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in json.dumps(scenario.tools[0]["parameters"])
        assert "oneOf" not in scenario.tools[0]["parameters"]
        contracts[PROBE.HISTORY_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.HISTORY_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert PROBE._history_scenario("overview_default").expected_arguments == {"action": "overview"}
    assert PROBE._history_scenario("search_default").expected_arguments == {
        "action": "search",
        "query": "deployment failure",
    }
    assert PROBE._history_scenario("read_default").expected_arguments == {"action": "read"}
    assert PROBE._history_scenario("around_default").expected_arguments == {
        "action": "around",
        "message_id": "message-123",
    }
    for action in ("overview", "search", "read", "around"):
        assert PROBE._history_scenario(f"{action}_cursor").expected_arguments == {
            "action": action,
            "cursor": "opaque-history-cursor",
        }


def test_ha_list_entities_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.HA_LIST_ENTITIES_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="ha_list_entities",
                ha_list_entities_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.HA_LIST_ENTITIES_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in scenario.tools[0]["parameters"]
        contracts[PROBE.HA_LIST_ENTITIES_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.HA_LIST_ENTITIES_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert PROBE._ha_list_entities_scenario("default").expected_arguments == {}
    assert PROBE._ha_list_entities_scenario("all").expected_arguments == {
        "domain": "climate",
        "area": "Upstairs",
    }


def test_ha_get_state_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.HA_GET_STATE_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="ha_get_state",
                ha_get_state_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.HA_GET_STATE_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in scenario.tools[0]["parameters"]
        contracts[PROBE.HA_GET_STATE_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.HA_GET_STATE_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert PROBE._ha_get_state_scenario("light").expected_arguments == {
        "entity_id": "light.living_room"
    }


def test_ha_list_services_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.HA_LIST_SERVICES_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="ha_list_services",
                ha_list_services_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.HA_LIST_SERVICES_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in scenario.tools[0]["parameters"]
        contracts[PROBE.HA_LIST_SERVICES_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.HA_LIST_SERVICES_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert PROBE._ha_list_services_scenario("default").expected_arguments == {}
    assert PROBE._ha_list_services_scenario("domain").expected_arguments == {"domain": "climate"}


def test_ha_call_service_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.HA_CALL_SERVICE_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="ha_call_service",
                ha_call_service_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.HA_CALL_SERVICE_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in scenario.tools[0]["parameters"]
        contracts[PROBE.HA_CALL_SERVICE_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.HA_CALL_SERVICE_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert PROBE._ha_call_service_scenario("base").expected_arguments == {
        "domain": "light",
        "service": "turn_on",
    }
    assert PROBE._ha_call_service_scenario("empty_data").expected_arguments["data"] == {}
    assert PROBE._ha_call_service_scenario("all").expected_arguments == {
        "domain": "light",
        "service": "turn_on",
        "entity_id": "light.living_room",
        "data": {"brightness": 180, "rgb_color": [255, 120, 40]},
    }


def test_image_generation_cases_use_production_profiles_and_exact_arguments() -> None:
    for case_name in PROBE.IMAGE_GENERATION_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="image_generation",
                image_generation_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments
        expected_schema = (
            PROBE.IMAGE_GENERATION_TEXT_ONLY_TOOL_PARAMETERS
            if case_name.startswith("text_")
            else PROBE.IMAGE_GENERATION_TOOL_PARAMETERS
        )

        assert scenario.tools[0]["parameters"] is expected_schema
        assert arguments is not None
        contracts[PROBE.IMAGE_GENERATION_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.IMAGE_GENERATION_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )


def test_memory_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.MEMORY_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="memory",
                memory_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.MEMORY_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.MEMORY_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.MEMORY_TOOL_NAME, "arguments": arguments}],
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


def test_session_read_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.SESSION_READ_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="session_read",
                session_read_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.SESSION_READ_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.SESSION_READ_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.SESSION_READ_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    whole = PROBE._session_read_scenario("whole")
    assert whole.expected_arguments == {"session_id": "session-123"}


def test_session_search_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.SESSION_SEARCH_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="session_search",
                session_search_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.SESSION_SEARCH_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.SESSION_SEARCH_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.SESSION_SEARCH_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    listing = PROBE._session_search_scenario("list")
    assert listing.expected_arguments == {}


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


def test_status_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.STATUS_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="status",
                status_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.STATUS_TOOL_PARAMETERS
        assert arguments is not None
        contracts[PROBE.STATUS_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.STATUS_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    current = PROBE._status_scenario("current")
    assert current.expected_arguments == {}


def test_subagent_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in PROBE.SUBAGENT_CASES:
        scenario = PROBE._scenario(
            SimpleNamespace(
                scenario="subagent",
                subagent_case=case_name,
                lines=8,
            ),
        )
        contracts = PROBE._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is PROBE.SUBAGENT_TOOL_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in json.dumps(scenario.tools[0]["parameters"])
        assert "oneOf" not in scenario.tools[0]["parameters"]
        contracts[PROBE.SUBAGENT_TOOL_NAME].validate_arguments(arguments)
        assert (
            PROBE._expected_argument_measurements(
                [{"name": PROBE.SUBAGENT_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert PROBE._subagent_scenario("run_self").expected_arguments == {
        "action": "run",
        "content": "Inspect the Tool contract and report concise findings.",
    }
    assert (
        PROBE._subagent_scenario("thinking_minimal").expected_arguments["thinking_effort"]
        == "minimal"
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


def test_mcp_probe_uses_the_production_definition_for_every_case():
    from resources.extensions.mcp.extension import MCP_DESCRIPTION, MCP_PARAMETERS

    for name, arguments in PROBE.MCP_CASE_ARGUMENTS.items():
        scenario = PROBE._mcp_scenario(name)
        assert scenario.tools == [
            {"name": "mcp_example", "description": MCP_DESCRIPTION, "parameters": MCP_PARAMETERS}
        ]
        assert scenario.expected_arguments == arguments
        assert scenario.require_closed_input is False


def test_computer_probe_uses_production_definition_and_validates_matrix():
    from resources.extensions.computer_use.extension import (
        COMPUTER_PARAMETERS,
        InvalidComputerArgumentsError,
        _validate_arguments,
    )
    from resources.extensions.computer_use.observations import Observation

    for case, expected in PROBE.COMPUTER_CASE_ARGUMENTS.items():
        args = PROBE._parser().parse_args(["--scenario", "computer", "--computer-case", case])
        scenario = PROBE._scenario(args)
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
    incomplete = asyncio.run(PROBE._probe_swarm_tool(Adapter(False), args))
    assert incomplete["feedback_received"]
    assert not incomplete["passed"]
    complete = asyncio.run(PROBE._probe_swarm_tool(Adapter(True), args))
    assert complete["passed"]
    assert complete["published_after_feedback"]


def test_patch_probe_rejects_importing_a_different_checkout(monkeypatch):
    import pytest

    from core.tools import apply_patch as patch_module

    args = PROBE._parser().parse_args(["--scenario", "apply_patch"])
    monkeypatch.setattr(
        patch_module, "__file__", str(PROJECT_ROOT / "wrong/core/tools/apply_patch.py")
    )
    with pytest.raises(RuntimeError):
        asyncio.run(PROBE._probe_apply_patch(None, args))
