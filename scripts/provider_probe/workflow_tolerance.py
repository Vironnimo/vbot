"""Agent-error recovery probes using production dispatch and disposable Skills."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.skills import SkillRegistry
from core.skills.authoring import SkillAuthoringService
from core.tools import ToolContext, ToolRegistry, register_skill_manage_tool, tool_failure
from core.tools.skill import register_skill_tool
from scripts.provider_probe.choices import SKILL_MANAGE_CASES
from scripts.provider_probe.scenario_agents import _skill_manage_scenario


def skill_tolerance_cases() -> list[dict[str, Any]]:
    cases = []
    for name in SKILL_MANAGE_CASES:
        scenario = _skill_manage_scenario(name)
        cases.append({"id": name, "arguments": scenario.expected_arguments})
    base = {
        "action": "write_file",
        "name": "provider-probe",
        "file_path": "assets/placeholder.txt",
        "content": "",
    }
    cases.extend(
        [
            {"id": "path_backslash", "arguments": {**base, "file_path": "assets\\placeholder.txt"}},
            {
                "id": "path_quoted",
                "arguments": {
                    **base,
                    "name": " provider-probe ",
                    "file_path": '"assets/placeholder.txt"',
                },
            },
            {"id": "spelling", "arguments": {**base, "action": " WRITE-FILE "}},
            {
                "id": "field_typo",
                "arguments": {k if k != "file_path" else "file_pth": v for k, v in base.items()},
            },
            {"id": "request_wrapper", "arguments": {"request": base}},
            {
                "id": "action_wrapper",
                "arguments": {"write_file": {k: v for k, v in base.items() if k != "action"}},
            },
            {
                "id": "operation_alias",
                "arguments": {k if k != "action" else "operation": v for k, v in base.items()},
            },
            {
                "id": "natural_patch",
                "task": (
                    "Lies references/notes.md im privaten Skill provider-probe und entferne nur "
                    "die veraltete Zeile. Der übrige Inhalt soll erhalten bleiben."
                ),
            },
            {
                "id": "natural_empty",
                "task": (
                    "Leere assets/placeholder.txt im privaten Skill provider-probe. "
                    "Die Datei soll bestehen bleiben und danach vollständig leer sein."
                ),
            },
        ]
    )
    return cases


async def _skill_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    with TemporaryDirectory(prefix="vbot-tolerance-") as temporary:
        root = Path(temporary)
        skills_root = root / "skills"
        authoring = SkillAuthoringService()
        original = _skill_manage_scenario("create_own").expected_arguments
        assert original is not None
        if case["id"] != "create_own":
            authoring.create(skills_root, "provider-probe", original["content"], author="agent")
            for path, content in {
                "scripts/check.py": "value = 1\n",
                "references/notes.md": "Keep this step.\nobsolete line\n",
                "assets/placeholder.txt": "old content",
            }.items():
                authoring.write_file(skills_root, "provider-probe", path, content)
        registry = ToolRegistry()
        register_skill_manage_tool(registry, authoring, lambda _: skills_root, lambda _: None)
        register_skill_tool(registry, lambda *_: SkillRegistry.load(skills_root), lambda: None)
        context = ToolContext(
            agent_id="probe",
            session_id="probe",
            run_id="probe",
            tool_call_id="probe",
            tool_name="skill_manage",
            tool_call_index=0,
            workspace=root,
            vbot_root=root,
            data_root=root,
        )
        task = case.get("task") or (
            "Execute this requested Skill operation once; "
            "equivalent representations are acceptable: " + json.dumps(case["arguments"])
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "Complete the user's task using the available Tools. Only this "
                    "disposable private Skill is in scope. Report what actually happened. "
                    "Available Skill: provider-probe (Provider testing instructions)."
                ),
            },
            {"role": "user", "content": task},
        ]
        observed = []
        final = ""
        for _ in range(8):
            raw = await adapter.send(
                messages,
                model_id=args.model,
                tools=registry.provider_definitions(),
                thinking_effort=args.thinking_effort,
                max_tokens=args.max_tokens or 3000,
            )
            response = adapter.normalize_response(raw, model_id=args.model)
            messages.append(
                {
                    "role": "assistant",
                    **{
                        k: response[k]
                        for k in ("content", "tool_calls", "reasoning", "reasoning_meta")
                        if k in response
                    },
                }
            )
            calls = response.get("tool_calls") or []
            if not calls:
                final = str(response.get("content") or "")
                break
            for call in calls:
                try:
                    result = await registry.dispatch(
                        replace(context, tool_name=call["name"], tool_call_id=call["id"]),
                        call["arguments"],
                    )
                except (ValueError, KeyError) as error:
                    result = tool_failure("invalid_arguments", str(error))
                observed.append({"call": call, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "name": call["name"],
                        "tool_call_id": call["id"],
                        "content": json.dumps(result),
                    }
                )
        files = {
            p.relative_to(skills_root).as_posix(): p.read_text(encoding="utf-8")
            for p in skills_root.rglob("*")
            if p.is_file()
        }
        canonical = registry.get("skill_manage").contract
        normalize = (
            registry.get("skill_manage").argument_normalizer or canonical.normalize_arguments
        )
        writes = [item for item in observed if item["call"]["name"] == "skill_manage"]
        checks = {
            "finished": bool(final),
            "successful_mutation": bool(writes) and all(item["result"]["ok"] for item in writes),
        }
        if "arguments" in case:
            expected = normalize(case["arguments"])
            checks["call_intent"] = (
                len(writes) == 1 and normalize(writes[0]["call"]["arguments"]) == expected
            )
            action = expected["action"]
            target = "provider-probe/" + expected.get("file_path", "SKILL.md")
            if action in {"create", "edit"}:
                checks["file_effect"] = expected["content"].split("---", 2)[
                    -1
                ].strip() in files.get(target, "")
            elif action == "write_file":
                checks["file_effect"] = files.get(target) == expected["content"]
            elif action == "patch":
                checks["file_effect"] = expected["old_string"] not in files.get(
                    target, ""
                ) and expected["new_string"] in files.get(target, "")
            elif action == "remove_file":
                checks["file_effect"] = target not in files
            else:
                checks["file_effect"] = not files
        elif case["id"] == "natural_patch":
            checks["file_effect"] = (
                files.get("provider-probe/references/notes.md") == "Keep this step.\n"
            )
        else:
            checks["file_effect"] = files.get("provider-probe/assets/placeholder.txt") == ""
        return {
            "case": case["id"],
            "passed": all(checks.values()),
            "checks": checks,
            "observed": observed,
            "final": final,
            "files": files,
        }


async def _probe_tool_tolerance(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    if args.tolerance_tool == "cron":
        from scripts.provider_probe.workflow_cron_tolerance import probe_cron_tolerance

        return await probe_cron_tolerance(adapter, args)
    if args.tolerance_tool == "skill":
        from scripts.provider_probe.workflow_skill_read_tolerance import (
            skill_read_case,
            skill_read_cases,
        )

        runner, cases = skill_read_case, skill_read_cases()
    elif args.tolerance_tool == "web_search":
        from scripts.provider_probe.workflow_search_tolerance import (
            search_case,
            search_tolerance_cases,
        )

        runner, cases = search_case, search_tolerance_cases()
    elif args.tolerance_tool == "status":
        from scripts.provider_probe.workflow_status_tolerance import (
            status_case,
            status_tolerance_cases,
        )

        runner, cases = status_case, status_tolerance_cases()
    elif args.tolerance_tool in {
        "ha_call_service",
        "ha_get_state",
        "ha_list_entities",
        "ha_list_services",
    }:
        from scripts.provider_probe.workflow_ha_tolerance import ha_case, ha_tolerance_cases

        runner, cases = ha_case, ha_tolerance_cases(args.tolerance_tool)
    elif args.tolerance_tool == "web_fetch":
        from scripts.provider_probe.workflow_web_tolerance import web_case, web_tolerance_cases

        runner, cases = web_case, web_tolerance_cases()
    elif args.tolerance_tool == "channel_send":
        from scripts.provider_probe.workflow_channel_tolerance import (
            channel_case,
            channel_tolerance_cases,
        )

        runner, cases = channel_case, channel_tolerance_cases()
    else:
        runner, cases = _skill_case, skill_tolerance_cases()
    rows = []
    semaphore = asyncio.Semaphore(
        1
        if args.tolerance_tool
        in {
            "web_fetch",
            "web_search",
            "ha_call_service",
            "ha_get_state",
            "ha_list_entities",
            "ha_list_services",
        }
        else 3
    )

    async def run(case: dict[str, Any]) -> None:
        async with semaphore:
            try:
                async with asyncio.timeout(args.total_timeout):
                    row = await runner(adapter, args, case)
            except Exception as error:
                row = {"case": case["id"], "passed": False, "error": str(error)}
            rows.append(row)
            if args.tolerance_report:
                args.tolerance_report.write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
                )

    await asyncio.gather(
        *(run(case) for case in cases if args.tolerance_case in {"all", case["id"]})
    )
    return {
        "scenario": "tool_tolerance",
        "passed": all(row["passed"] for row in rows),
        "cases": [{k: v for k, v in row.items() if k not in {"observed", "files"}} for row in rows],
    }
