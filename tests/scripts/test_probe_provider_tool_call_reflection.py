"""Provider diagnostic probe reflection coverage."""

from __future__ import annotations

import asyncio
import json

import scripts.provider_probe.workflow_reflection as probe_workflow_reflection
from tests.scripts.provider_probe_helpers import PROBE


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
    case = next(
        case for case in probe_workflow_reflection._reflection_cases() if case["id"] == case_id
    )
    args = PROBE._parser().parse_args(["--scenario", "reflection_workflow"])
    adapter = _ReflectionAdapter(calls)
    result = asyncio.run(
        probe_workflow_reflection._probe_reflection_case(adapter, args, case, scope)
    )
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
        probe_workflow_reflection,
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

    monkeypatch.setattr(probe_workflow_reflection, "_probe_reflection_case", evaluate)
    args = PROBE._parser().parse_args(["--scenario", "reflection_workflow"])
    result = asyncio.run(probe_workflow_reflection._probe_reflection_workflow(None, args))
    assert result["passed"] is False
    assert result["cases"] == [
        {"case": "fails", "scope": "memory", "passed": False, "error_type": "ValueError"},
        {"case": "finishes", "scope": "skill", "passed": True},
    ]
