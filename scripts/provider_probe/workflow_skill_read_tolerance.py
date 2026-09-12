"""Skill listing, activation, and package read probes against actual fixture files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.skills import SkillRegistry
from core.skills.authoring import SkillAuthoringService
from core.tools import ToolContext, ToolRegistry, tool_failure
from core.tools.skill import register_skill_tool
from scripts.provider_probe.choices import SKILL_CASES
from scripts.provider_probe.scenario_agents import _skill_scenario


def skill_read_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"id": name, "arguments": _skill_scenario(name).expected_arguments} for name in SKILL_CASES
    ]
    cases.extend(
        [
            {"id": "empty_selection", "arguments": {"name": "  "}},
            {
                "id": "empty_target",
                "arguments": {"name": "  ", "file_path": "SKILL.md"},
                "success": False,
            },
            {
                "id": "backslash",
                "arguments": {"name": "vbot-cli", "file_path": "references\\commands.md"},
            },
            {
                "id": "quoted",
                "arguments": {"name": " vbot-cli ", "filePath": '"assets/template.txt"'},
            },
            {
                "id": "traversal",
                "arguments": {"name": "vbot-cli", "file_path": "assets/../../outside"},
                "success": False,
            },
        ]
    )
    return cases


async def skill_read_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    with TemporaryDirectory(prefix="vbot-skill-read-tolerance-") as temporary:
        root = Path(temporary)
        content = (
            "---\nname: vbot-cli\ndescription: Fixture instructions.\n---\nRead fixture notes.\n"
        )
        authoring = SkillAuthoringService()
        authoring.create(root, "vbot-cli", content, author="agent")
        for path in ("references/commands.md", "scripts/run.py", "assets/template.txt"):
            authoring.write_file(root, "vbot-cli", path, "fixture content")
        registry = ToolRegistry()
        register_skill_tool(registry, lambda *_: SkillRegistry.load(root), lambda: None)
        raw = await adapter.send(
            [
                {
                    "role": "system",
                    "content": "Make one diagnostic Tool Call with all supplied "
                    "fields. Preserve parsed JSON values, including quotes and backslashes inside "
                    "path values. Omit fields not supplied. Only the fixture Skill is in scope.",
                },
                {"role": "user", "content": "Arguments: " + json.dumps(case["arguments"])},
            ],
            tools=registry.provider_definitions(),
            model_id=args.model,
            thinking_effort=args.thinking_effort,
            max_tokens=args.max_tokens or 2500,
        )
        calls = adapter.normalize_response(raw, model_id=args.model).get("tool_calls") or []
        results = []
        for call in calls:
            context = ToolContext(
                agent_id="probe",
                session_id="probe",
                run_id="probe",
                tool_call_id=call["id"],
                tool_name=call["name"],
                tool_call_index=0,
                workspace=root,
                vbot_root=root,
                data_root=root,
            )
            try:
                results.append(await registry.dispatch(context, call["arguments"], ["skill"]))
            except ValueError as error:
                results.append(tool_failure("invalid_arguments", str(error)))
        checks = {"one_call": len(calls) == len(results) == 1}
        data = results[0].get("data") or {} if results else {}
        if not case.get("success", True):
            checks["rejected"] = bool(results) and not results[0]["ok"]
        elif case["id"] in {"list", "empty_selection"}:
            checks["catalog"] = data.get("count") == 1 and "vbot-cli" in json.dumps(data)
        elif case["id"] == "activate":
            checks["activation"] = data.get("status") == "loaded" and (
                "Read fixture notes." in data.get("content", "")
            )
        else:
            request = case["arguments"]
            path = request.get("file_path", request.get("filePath")).strip('"').replace("\\", "/")
            checks["file_content"] = data.get("content") == (root / "vbot-cli" / path).read_text(
                encoding="utf-8"
            )
            checks["path"] = data.get("file_path") == path
        return {
            "case": case["id"],
            "passed": all(checks.values()),
            "checks": checks,
            "observed": calls,
            "results": results,
        }
